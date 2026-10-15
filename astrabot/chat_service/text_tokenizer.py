"""中英文分词与关键词提取。

不依赖第三方分词库（如 jieba）：本项目所在的 Windows 环境启用了
智能应用控制，会拦截部分原生扩展 DLL（此前 orjson 就被拦截过），
因此这里用纯 Python 实现，避免再引入带 C 扩展的依赖。

策略：
- 英文/数字：按单词切分，转小写
- 中文：双字滑窗（bigram）+ 单字兜底。中文里双字组合的区分度
  远高于单字，且不需要词典即可覆盖大部分检索场景。
- 过滤停用词与纯标点
"""

from __future__ import annotations

import re

# 中英文停用词：这些词在检索时几乎不提供区分度
_STOPWORDS = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "那", "什么", "怎么", "这个", "那个",
    "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "哈", "把", "被", "给", "让",
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "in",
    "on", "at", "for", "and", "or", "it", "this", "that", "with", "as",
}

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[a-zA-Z0-9_]+")


def _cjk_grams(segment: str) -> list[str]:
    """中文片段切分为 bigram（不足两字时退化为单字）。"""
    grams: list[str] = []
    n = len(segment)
    if n == 1:
        return [segment]
    for i in range(n - 1):
        grams.append(segment[i : i + 2])
    return grams


def tokenize(text: str) -> list[str]:
    """把文本切成用于检索的词元列表（保持出现顺序、去重）。"""
    if not text:
        return []

    tokens: list[str] = []
    seen: set[str] = set()

    def push(tok: str) -> None:
        tok = tok.strip().lower()
        if not tok or tok in _STOPWORDS or len(tok) < 2:
            return
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    # 先按中文片段与非中文交替处理
    pos = 0
    for m in re.finditer(r"[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+", text):
        chunk = m.group(0)
        if _CJK.fullmatch(chunk):
            for g in _cjk_grams(chunk):
                push(g)
        else:
            for w in _ASCII_WORD.findall(chunk):
                push(w)
        pos = m.end()

    return tokens


def extract_keywords(text: str, limit: int = 12) -> str:
    """提取关键词串，供 LIKE 检索使用（空格分隔）。"""
    return " ".join(tokenize(text)[:limit])


def term_list(text: str, limit: int = 12) -> list[str]:
    """返回关键词列表（不拼成字符串）。"""
    return tokenize(text)[:limit]
