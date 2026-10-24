"""表情包本地库回归测试：魔数校验 / 入库拒收 / 启动自检隔离 / 本地镜像解析。

背景 bug：message_handler 把历史图片的 URL 当 base64 解码后存进本地库，
产出 165~171 字节的确定性垃圾；resolve_sticker 本地优先，于是每次都选中
垃圾，发送被 QQ 图床以 error_code=921 拒绝。纯打桩，不联网、不调 LLM。
无 pytest 时可 `python tests/test_sticker_local.py` 直跑。"""

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.chat_service import message_handler as mh
from astrabot.chat_service import sticker_gallery as sg
from astrabot.chat_service import sticker_store as ss
from astrabot.chat_service.image_utils import image_extension, is_image_bytes

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
# 实际污染样本的头 16 字节（base64 解码 URL 得到的垃圾）
POISON = bytes.fromhex("86db69b3ffe6ba5b6299e7626a7b6aa9") + b"\x00" * 4
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 20


class isolated:
    """把两个模块的落盘路径与内存缓存全指到临时目录。"""

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory(prefix="astrabot-sticker-test-")
        root = Path(self.dir.name)
        self._saved = (ss.DATA_DIR, ss.STICKER_DIR, ss.INDEX_FILE, ss.REJECTED_DIR, sg._IMAGE_DIR)
        ss.DATA_DIR = root
        ss.STICKER_DIR = root / "stickers"
        ss.INDEX_FILE = ss.STICKER_DIR / "index.json"
        ss.REJECTED_DIR = root / "stickers_rejected"
        sg._IMAGE_DIR = root / "gallery_images"
        ss._index_cache = None
        ss._index_mtime = 0.0
        ss._list_cache = None
        ss._list_cache_mtime = -1.0
        return root

    def __exit__(self, *exc):
        (
            ss.DATA_DIR,
            ss.STICKER_DIR,
            ss.INDEX_FILE,
            ss.REJECTED_DIR,
            sg._IMAGE_DIR,
        ) = self._saved
        ss._index_cache = None
        ss._list_cache = None
        self.dir.cleanup()


def test_magic_checks():
    assert is_image_bytes(JPEG) and image_extension(JPEG) == ".jpg"
    assert is_image_bytes(PNG) and image_extension(PNG) == ".png"
    assert is_image_bytes(GIF) and image_extension(GIF) == ".gif"
    assert is_image_bytes(WEBP) and image_extension(WEBP) == ".webp"
    # 关键：污染样本必须被认出来并拒收
    assert not is_image_bytes(POISON), "URL-decode 垃圾应被魔数校验挡下"
    assert image_extension(POISON) == ""
    assert not is_image_bytes(b"") and not is_image_bytes(b"\xff\xd8\xff")
    # 复现线上动作：把历史图片 URL 丢进真实解码入口。线上 URL 恰好可解，
    # 得到 165~171 字节垃圾；换一条不可解的则返回 None。两种都必须不是图片。
    for url in (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=EvVV"
        "PgAAEwAA&openim_process_type=1",
        "http://tdjc.qianqianlife.com/pic/2022-06-23/abcdefg.jpg",
    ):
        raw = ss.image_b64_to_bytes(url)
        assert raw is None or not is_image_bytes(raw), "URL 当 b64 解码不得产出图片"


def test_save_rejects_non_image_and_keeps_extension():
    with isolated():
        assert ss.save_sticker(POISON, "垃圾图") is None, "非图片不得入库"
        assert ss.all_stickers() == [] and ss._load_index() == {}

        sid_png = ss.save_sticker(PNG, "一张 png 表情包")
        assert sid_png, "合法 png 应入库"
        meta = ss._load_index()[sid_png]
        assert meta["file"].endswith(".png"), f"扩展名应按真实格式，得到 {meta['file']}"

        sid_gif = ss.save_sticker(GIF, "一张 gif 表情包")
        assert ss._load_index()[sid_gif]["file"].endswith(".gif")
        # 没有描述的图无法让模型按语境挑选，照旧拒收
        assert ss.save_sticker(JPEG, "   ") is None


def test_sanitize_quarantines_poisoned_entries():
    with isolated():
        # 手工造一个被污染的库：一条真图 + 一条垃圾 + 一条文件丢失
        ss.STICKER_DIR.mkdir(parents=True, exist_ok=True)
        (ss.STICKER_DIR / "good00000000.jpg").write_bytes(JPEG)
        (ss.STICKER_DIR / "bad000000000.jpg").write_bytes(POISON)
        index = {
            "good00000000": {"file": "good00000000.jpg", "description": "真图", "created_at": 1, "uses": 3},
            "bad000000000": {"file": "bad000000000.jpg", "description": "垃圾", "created_at": 2, "uses": 9},
            "gone00000000": {"file": "gone00000000.jpg", "description": "文件已丢", "created_at": 3, "uses": 0},
        }
        ss.INDEX_FILE.write_text(json.dumps(index), encoding="utf-8")

        report = ss.sanitize()
        assert report["kept"] == 1 and report["dropped"] == 2, report
        left = json.loads(ss.INDEX_FILE.read_text(encoding="utf-8"))
        assert set(left) == {"good00000000"}, "索引里只应留下真图"
        assert (ss.REJECTED_DIR / "bad000000000.jpg").is_file(), "垃圾文件应被移出而非删除"
        assert not (ss.STICKER_DIR / "bad000000000.jpg").exists()

        # dry_run 只统计，不动盘也不动文件
        ss._save_index(index)
        (ss.STICKER_DIR / "bad000000000.jpg").write_bytes(POISON)
        dry = ss.sanitize(dry_run=True)
        assert dry["dropped"] == 2, dry
        assert set(json.loads(ss.INDEX_FILE.read_text(encoding="utf-8"))) == set(index)
        assert (ss.STICKER_DIR / "bad000000000.jpg").is_file()


def test_local_image_path_is_safe():
    with isolated():
        sg._IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        (sg._IMAGE_DIR / "abc123def456.gif").write_bytes(GIF)
        assert sg.local_image_path("abc123def456") == sg._IMAGE_DIR / "abc123def456.gif"
        assert sg.local_image_path("  abc123def456  ") == sg._IMAGE_DIR / "abc123def456.gif"
        assert sg.local_image_path("missing00000") is None
        assert sg.local_image_path(None) is None
        # 路径穿越与带分隔符的 id 一律拒绝
        assert sg.local_image_path("../../etc/passwd") is None
        assert sg.local_image_path("abc.tmp") is None


def _stub_download(payload, calls):
    async def fake(url: str, timeout: int = 10):
        calls.append(url)
        return payload

    return fake


def test_prefetch_writes_only_valid_images():
    entries = [{"id": "img000000001", "url": "https://raw.githubusercontent.com/x/y/main/images/a.png"}]
    saved_ls, saved_dl = sg.list_stickers, sg.download_image_via_proxy
    try:
        sg.list_stickers = lambda: asyncio.sleep(0, result=entries)
        with isolated():
            calls: list[str] = []
            sg.download_image_via_proxy = _stub_download(PNG, calls)
            out = asyncio.run(sg.prefetch_local())
            assert out == {"total": 1, "fetched": 1, "failed": 0}, out
            p = sg.local_image_path("img000000001")
            assert p is not None and p.suffix == ".png" and p.read_bytes() == PNG

            # 已有文件不再重复下载
            calls.clear()
            assert asyncio.run(sg.prefetch_local())["fetched"] == 0 and calls == []

            # 强制重下时垃圾字节不落盘，原文件也不被污染
            sg.download_image_via_proxy = _stub_download(POISON, calls)
            out3 = asyncio.run(sg.prefetch_local(force=True))
            assert out3["failed"] == 1, out3
            assert sg.local_image_path("img000000001").read_bytes() == PNG

            # 下载失败（None）同样只算失败，不写半个文件
            p.unlink()
            sg.download_image_via_proxy = _stub_download(None, calls)
            assert asyncio.run(sg.prefetch_local(force=True))["failed"] == 1
            assert sg.local_image_path("img000000001") is None
    finally:
        sg.list_stickers = saved_ls
        sg.download_image_via_proxy = saved_dl


def test_resolve_prefers_local_then_mirror_then_remote():
    entries = [{"id": "remote000001", "url": "https://raw.githubusercontent.com/x/y/main/images/a.jpg"}]
    saved_ls, saved_mirror = sg.list_stickers, sg.schedule_mirror
    try:
        sg.list_stickers = lambda: asyncio.sleep(0, result=entries)
        hits: list[int] = []
        sg.schedule_mirror = lambda: hits.append(1)
        with isolated():
            # 1) 自建本地库优先
            sid = ss.save_sticker(JPEG, "群里的图")
            assert asyncio.run(sg.resolve_sticker(sid)) == ss.STICKER_DIR / f"{sid}.jpg"
            # 2) 本地镜像命中，完全不碰远程
            sg._IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            (sg._IMAGE_DIR / "mirror0000001.gif").write_bytes(GIF)
            assert asyncio.run(sg.resolve_sticker("mirror0000001")) == sg._IMAGE_DIR / "mirror0000001.gif"
            # 3) 都没有 → 远程 url，并触发一次后台补齐
            assert asyncio.run(sg.resolve_sticker("remote000001")) == entries[0]["url"]
            assert hits == [1], "走远程时应顺带触发镜像补齐"
            # 4) 空/null 不解析
            for bad in (None, "", "null", "None"):
                assert asyncio.run(sg.resolve_sticker(bad)) is None
    finally:
        sg.list_stickers = saved_ls
        sg.schedule_mirror = saved_mirror


def test_vision_desc_lookup():
    r = {"image_1": "第一张", "image_2": "第二张"}
    assert mh._vision_desc_at(r, 0) == "第一张"
    assert mh._vision_desc_at(r, 1) == "第二张"
    assert mh._vision_desc_at(r, 2) == "", "超出范围不得串描述"
    assert mh._vision_desc_at({}, 0) == "" and mh._vision_desc_at(None, 0) == ""
    # 模型不按约定给键时退化为"排序后按位置取"（旧行为）
    assert mh._vision_desc_at({"a": "甲", "b": "乙"}, 0) == "甲"


def test_handler_no_longer_decodes_urls():
    """源头护栏：入库必须用真正下载到的 b64，不能再碰 map 里的 URL。"""
    text = Path(mh.__file__).read_text(encoding="utf-8")
    assert "ok_pairs" in text, "识别结果与图片位置的绑定被改没了"
    assert "for (_, _, b64), (_, desc) in zip(" not in text, "又拿 map 的第三项当 b64 了"
    assert "for pos, b64 in ok_pairs" in text
    assert "is_image_bytes(payload)" in text, "发送前的魔数校验被改没了"


def test_schedule_mirror_cooldown():
    """GitHub 不通时，重复触发只能补一次，不能被 30 个必败下载刷屏。"""
    saved_ts, saved_task, saved_safe = (
        sg._mirror_last_attempt,
        sg._prefetch_task,
        sg._mirror_safe,
    )
    calls: list[int] = []

    async def fake_mirror():
        calls.append(1)

    async def run():
        sg._mirror_safe = fake_mirror
        sg._prefetch_task = None
        sg._mirror_last_attempt = 0.0
        sg.schedule_mirror()
        first = sg._prefetch_task
        await asyncio.sleep(0)
        assert calls == [1], "启动那次应立刻补齐"
        if first:
            await first
        sg.schedule_mirror()
        assert calls == [1], "冷却期内重复触发必须跳过"
        sg._mirror_last_attempt -= sg._MIRROR_COOLDOWN + 1
        sg.schedule_mirror()
        await asyncio.sleep(0)
        assert len(calls) == 2, "冷却结束后应恢复补齐"
        trailing = sg._prefetch_task
        if trailing:
            await trailing

    try:
        asyncio.run(run())
    finally:
        sg._mirror_safe = saved_safe
        sg._prefetch_task = None
        sg._mirror_last_attempt = saved_ts


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback

            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
