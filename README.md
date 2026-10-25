# AstraBot

基于 [NoneBot2](https://nonebot.dev/) + [OneBot V11](https://github.com/botuniverse/onebot-11) 的 QQ 群聊机器人，支持多 AI 厂商、三层记忆系统、插件机制和联网搜索。

## 功能特性

- **多 AI 厂商支持** — MiniMax / DeepSeek / SiliconFlow，可配置主备切换
- **三层记忆系统** — 短期（近期对话上下文）、中期（自动提取的事实按相关性检索）、长期（用户指定记住的内容，始终加载）
- **插件机制** — 动态加载 `plugins/` 目录下的插件，支持链式执行、提示词注入、跳过 AI 生成
- **联网搜索** — 多轮搜索 + 网页抓取 + 结果总结，搜索结果来自智谱原生 web_search 接口
- **图片分析** — 自动识别群聊图片并注入到 AI 上下文
- **回复概率控制** — 四种概率分别控制普通消息 / @触发 / 回复中搭话 / 回复中被 @ 插入
- **事实自动提取** — 从群聊对话中自动提取用户信息，形成中期记忆
- **记忆指令** — 支持 `记住` / `忘记` / `查看记忆` 等自然语言记忆管理
- **下线通知** — bot被踢下线，通过系统通知提醒下线（支持Linux/macOS/Windows）

## 项目结构

```
AstraBot/
├── astrabot/
│   ├── config.py                    # 全局配置，从 .env 加载
│   ├── chat_service/
│   │   ├── message_handler.py       # 消息处理核心（解析、上下文组装、AI 调用）
│   │   ├── ai_client.py             # 统一封装 MiniMax / DeepSeek / SiliconFlow API
│   │   ├── enhanced_memory.py       # 三层记忆系统（含对话事实自动提取）
│   │   ├── history_manager.py       # 群聊历史记录管理（SQLite + JSONL）
│   │   ├── memory_manager.py        # 记忆系统入口（LTM / MTM / STM 协调）
│   │   ├── memory_commands.py       # 自然语言记忆指令解析
│   │   ├── prompt_builder.py        # AI 提示词组装
│   │   ├── reply_controller.py      # 回复概率判定与状态管理
│   │   ├── search_engine.py         # 联网搜索与结果总结
│   │   ├── image_utils.py           # 图片解析工具
│   │   ├── plugin_loader.py         # 插件动态加载器
│   │   └── plugins/
│   │       └── example_plug/        # 示例插件模板
│   └── logmanager/
│       └── logger.py                # 统一日志管理
├── .env.example                     # 环境变量示例
├── .env.prod                        # 生产环境部署变量
├── pyproject.toml                   # 项目依赖与 NoneBot 配置
└── README_Plugin.md                 # 插件开发文档
```

## 快速开始

### 环境要求

- Python >= 3.10
- API Key：
  - **主聊天模型** — 通过 `API_KEY` 配置，同时用于联网搜索 Agent
    （智谱端点还顺带提供 `web_search` 联网搜索结果，无需额外密钥）
  - **视觉模型** — 识图按 `VISUAL_API_*` → `VISUAL_BACK_API_*` 两档依次尝试（前者主力、后者辅助），
    用于图像识别；主力档留空则回退主聊天模型，Key 留空则复用对应档位的聊天 Key

### 安装

```bash
# 对于 Linux 用户，请先确保你安装了libnotify：
pacman -Ss libnotify # 例如 ArchLinux，搜索结果会提示“已安装”
sudo pacman -S libnotify # 如果没安装，直接安装即可

# 先安装pipx
sudo pacman -S python-pipx # 不同发行版和系统不一样。例如Windows需要pip：python -m pip install --user pipx
pipx install nb-cli

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate # Windows用./.venv/scripts/activate；fish用户用source .venv/bin/activate.fish

# 安装依赖
pip install -e .
```

#### Windows 离线安装

若无网络，可先下载依赖轮子到 `wheels/`，再离线安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .\wheels -e .
```

> **重要**：请使用 python.org 官方 Windows 版 Python。MSYS2 / UCRT64 的 Python
> 平台标签为 `mingw_x86_64_ucrt_gnu`，**无法安装** PyPI 上的 `win_amd64` 轮子
> （`orjson`、`pydantic-core`、`jiter`、`cffi` 等编译型依赖会全部失败）。

### 配置

```bash
# 复制环境变量模板（Windows: copy .env.example .env）
cp .env.example .env

# 编辑 .env，填入 API Key 和配置
# 必填项：API_KEY, BACK_API_KEY, VISUAL_API_KEY
#          name, name_cn, person_setting, output_style, reply_style, extra_style, image_analyzer
#          API_PROTOCOL, API_BASE_URL, API_MODEL, BACK_API_PROTOCOL, BACK_API_BASE_URL, BACK_API_MODEL, VISUAL_API_MODEL
```

关键配置项说明：

| 变量 | 说明 |
|------|------|
| `name` / `name_cn` | 机器人名称 / 中文名 |
| `person_setting` | 角色人设（性别、年龄等） |
| `output_style` | 输出格式约束（JSON 格式，控制回复结构） |
| `reply_style` | 说话风格设定 |
| `enabled_groups` | 启用机器人的群号列表，如 `['12345678']` |
| `reply_rate` | 普通消息回复概率（0.0 ~ 1.0） |
| `reply_rate_at` | 被 @ 时的回复概率 |
| `API_PROTOCOL` / `API_BASE_URL` / `API_KEY` / `API_MODEL` | 主聊天模型配置，同时用于联网搜索 Agent |
| `BACK_API_PROTOCOL` / `BACK_API_BASE_URL` / `BACK_API_KEY` / `BACK_API_MODEL` | 主 API 失败时的备用聊天模型配置 |
| `VISUAL_API_KEY` / `VISUAL_API_PROVIDER` / `VISUAL_API_MODEL` | 识图主力端点（多模态模型） |
| `VISUAL_BACK_API_BASE_URL` / `VISUAL_BACK_API_KEY` / `VISUAL_BACK_API_MODEL` | 识图辅助端点：主力档限流/超时/返回非 JSON 时改试它，每档只试一次；三项留空则不参与 |
| `command_allowed_users` | 群内指令白名单 QQ 号，写 `123456,789012` 或 `[123456,789012]`；留空时删除/清空记忆与切换人设一律拒绝（见下面的「群内指令」） |

### 启动

你需要先安装napcatqq然后设置：
- 安装：https://napneko.github.io/guide/install
- 配置：https://napneko.github.io/config/basic

```bash
nb run
```

也可以直接运行入口文件（等价于 `nb run`）：

```bash
python bot.py
```

> **注意**：`nb run` 会在当前目录自动查找 `bot.py` 并运行它。项目根目录的 `bot.py`
> 会在 `nonebot.init()` 之前调用 `load_dotenv()`，确保 `.env` 中的 `DRIVER`、`HOST`、
> `PORT` 等配置能被正确读取。若省略这一步，驱动会退化为默认的 `~fastapi`，
> 它将**不支持 WebSocket 客户端连接**，导致无法连接 NapCat / SnowLuma。

如果 `nb` 未能识别本项目使用的虚拟环境（例如项目内同时存在多个 venv 目录），
可用 `-py` 显式指定解释器：

```bash
nb -py ./.venv/Scripts/python.exe run
```

更多启动方式请参考 [NoneBot 文档](https://nonebot.dev/docs/)。

## 群内指令

指令**只在群聊里解析**（私聊一律走 AI 对话）。斜杠指令要求**整条消息以 `/` 开头**——写在句子里
（如"帮我 /用量 看看"）不算指令，会被当成普通聊天交给 AI；`@机器人 /用量` 可以，at 段会先被剥掉。
进入指令通道后，除下面标注的三条按**前缀**识别，其余要求**整条消息正好等于指令本身**，多打一个字就成了
未知指令而被静默丢弃（不会回"指令不存在"）。唯一的例外是 `记住：` 一族，不带斜杠也认。

| 指令 | 作用 |
| --- | --- |
| `/人设`（`/persona`） | 查看本群当前人设与可选预设 |
| `/人设 键盘侠` | 把**本群**切换成指定预设，下一条消息起生效 |
| `/人设 默认` | 恢复 `.env` 里的 `person_setting` |
| `/用量` `/usage` `/cost` | 累计 API 开销；对话与联网搜索记在同一本账里，多于一个模型时按模型拆分 |
| `/用量 今日` `/usage today` | 只看今天 |
| `/用量 7天` `/usage 7d` | 只看近 7 天 |
| `记住：XXX` | 写入长期记忆；`记住 XXX`、`帮我记住：`、`请记住` 同样识别，**不需要斜杠** |
| `/memory list` `/记忆列表` | 列出长期记忆（带编号，删除时用这个编号） |
| `/memory facts` `/事实记忆` | 列出最近自动提取的事实记忆 |
| `/memory delete <id>` `/删除记忆 <id>` | 删除该编号条目：先找长期记忆，找不到再找事实记忆 |
| `/memory clear` `/清空事实记忆` | 只回显警告，**不删任何东西** |
| `/memory clear confirm` | 真正执行清空（仅本群的事实记忆，长期记忆保留） |
| `/memory help` `/记忆帮助` | 记忆相关指令的帮助 |

带参数的三条（`记住：…`、`/memory delete <id>`、`/人设 …`）按**前缀**识别，其余按整条相等识别。

**权限**（由 `command_allowed_users` 决定，见上面的配置表）：

- **已配置白名单**：只有名单内的 QQ 能执行任何指令；其他人的指令被**静默丢弃**，连拒绝都不会回。
- **未配置白名单**：查询类指令与 `记住：` 照常放行，但删除/清空记忆、切换人设一律 fail-safe 拒绝
  ——切换人设改的是机器人在整个群的说话方式，影响面比删一条记忆大。
- 人设与记忆都**按群存储**，互不影响；人设状态在 `astrabot/chat_service/data/personas.json`（gitignore 内）。
- 内置预设目前只有「键盘侠」，文本与它的红线约束都写在一个字符串里：
  [astrabot/chat_service/persona_store.py](astrabot/chat_service/persona_store.py) 的 `PRESETS`，想加新
  人设或调轻重，改这里即可（只接受预设名，不接受自由文本，否则任何人都能往系统提示词里塞内容）。

## 插件开发

插件放在 `astrabot/chat_service/plugins/` 目录下，每个插件一个子目录，包含 `__init__.py`（实现 `run()` 函数）和可选的 `settings.toml`。

```python
def run(bot, event, history, image_desc, config, plugin_config):
    """
    bot           - NoneBot Bot 实例
    event         - GroupMessageEvent
    history       - list[dict]，最近聊天记录
    image_desc    - str，当前消息的图片分析结果
    config        - Config 对象
    plugin_config - dict，settings.toml 内容

    返回值:
      None          - 不干预
      {"reply": "你好", "skip_main": True}   - 直接回复，跳过 AI
      {"append_prompt": "..."}               - 在 AI 提示词末尾追加内容
      {"override_prompt": "..."}             - 完全替换 AI 提示词
      {"block": True}                        - 阻止后续插件执行
    """
    return None
```

详见 [README_Plugin.md](./README_Plugin.md)。

## 许可证

GNU GPL v3.0