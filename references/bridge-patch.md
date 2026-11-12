# 接入补丁：把 overlay 接进上游 ilink.py

这个文件讲怎么把 `overlay/` 的七个文件接进上游仓库。只改 `ilink.py` 和 `AGENTS.md`，不要改上游原有行为。行号是移植当时 live 文件里的行号，上游版本不同时行号会漂移，按函数名和锚点文字找位置。

## 先装文件

把 `overlay/` 里的七个文件复制进桥目录的对应位置，见 `../overlay/README.md`。复制完再做下面的接线。

## ilink.py 的五处改动

### 1. usage 文本

位置：文件开头的模块 docstring，`ilink.py proactive-voice` 那一行后面。live 文件第 29 到 38 行。加下面十行：

```
ilink.py transcribe <media_path> [--model tiny] [--timeout 120]
ilink.py files [--limit N]
ilink.py files-find <keyword> [--limit N]
ilink.py todo-add <due> <text>
ilink.py todo-list [--all]
ilink.py todo-done <id>
ilink.py todo-show <id>
ilink.py todo-set-cron <id> <cron_id>
ilink.py todo-cancel <id>
ilink.py todo-cleanup [--days N]
```

每行后面保留一句英文注释，说明命令做什么，见 live 文件里的原文。

### 2. cmd_prune_media 里清理索引

位置：`cmd_prune_media` 函数，live 文件第 271 行起，插入点在清理循环之后、打印 `PRUNE_OK` 之前。live 文件第 340 到 350 行。加下面逻辑：

- 不是 dry run 时才做，`import file_index`，跑 `file_index.prune_missing()`，返回值记成 `index_dropped`。
- 整个调用包在 try 和 except 里，索引坏了也不许让清理本身失败，失败时 `index_dropped` 记 0。
- 输出行从 `deleted=... freed=... skipped=...` 后面追加 `index_dropped=...`。

### 3. cmd_poll 里记录收到的文件

位置：`cmd_poll` 函数，live 文件第 1776 行起。插入点在 `elif t in (2, 4, 5)` 这个 IMAGE、FILE、VIDEO 分支里，下载成功的那条路（`if p:` 之后），再往里一层是 `if t == 4:`，只对 FILE 类型记。live 文件第 1893 到 1902 行。加下面逻辑：

```
import file_index
file_index.record(
    m.get("message_id"), from_id,
    entry.get("file_name", ""),
    p, os.path.getsize(p), "file",
    snippet=file_index.extract_snippet(p))
```

整个调用包在 try 和 except 里，失败就 pass，不要打印任何东西。poll 的 stdout 是机器可读的 JSON 通道，多一行输出就会破坏整批消息。

### 4. cmd_transcribe 新函数

位置：在 `cmd_preflight` 定义之前新加一个函数 `cmd_transcribe(media_path, model="tiny", timeout=120)`，live 文件第 3526 行起。函数体逻辑：

- `from voice_transcribe import transcribe_voice`，ImportError 时打印 `TRANSCRIBE_FAILED:voice_transcribe missing: ...` 并返回 1。
- 跑 `transcribe_voice(media_path, model=model, timeout=timeout)`，返回 None 时打印 `TRANSCRIBE_FAILED:see stderr` 并返回 1。
- 返回空串时打印 `TRANSCRIBE_EMPTY` 并返回 0。
- 有文本时打印 `TRANSCRIBE_OK:<text>` 并返回 0。
- stdout 只打印上面那一行结果，诊断信息走 stderr。

### 5. main 里的分发

位置：`main(argv)` 函数，live 文件第 3630 行起。按顺序加四组分支：

- `transcribe` 分支加在 `if cmd == "preflight"` 那组之后。live 文件第 3693 行起。参数不足时 `_need(argv, 3, ...)` 返回 2。`--model` 和 `--timeout` 用 `_int_arg` 解析，解析失败返回 2。最后 `return cmd_transcribe(argv[2], model=model, timeout=timeout)`。
- `files` 和 `files-find` 分支加在 `proactive-voice` 那组之后。live 文件第 3796 行起。`files` 默认 limit 是 10，`--limit` 用 `_int_arg` 解析，调 `file_index.cmd_files(limit=limit)`。`files-find` 要求至少 3 个参数，调 `file_index.cmd_files_find(argv[2], limit=limit)`。
- 七个 `todo-*` 分支加在所有原有分支之后、`print(__doc__)` 之前。live 文件第 3821 行起。`todo-add` 要求至少 4 个参数，调 `todos.cmd_todo_add(argv[2], " ".join(argv[3:]))`。`todo-list` 调 `todos.cmd_todo_list(include_done="--all" in argv[2:])`。`todo-done`、`todo-show`、`todo-cancel` 各要求至少 3 个参数，分别调 `todos.cmd_todo_done`、`todos.cmd_todo_show`、`todos.cmd_todo_cancel`。`todo-set-cron` 要求至少 4 个参数，调 `todos.cmd_todo_set_cron(argv[2], argv[3])`。`todo-cleanup` 默认 30 天，`--days` 用 `_int_arg` 解析，调 `todos.cmd_todo_cleanup(days)`。
- `send-voice` 和 `proactive-voice` 的分发原样保留，不要删。它们已经实现但服务端会静默丢弃，契约里禁用，见 `AGENTS.md` 的 2.4 节。

## AGENTS.md 的三处改动

位置：live 文件里的行号是 2.4 节在第 237 行，2.5 节在第 257 行。

### 1. 文件索引段落

位置：第 2 节的 `Read what the user actually sent` 那条里，`never a summary guessed from the file name.` 这一句后面，`- Never manually retry a failed send` 这一条前面。加一段：收到文件在 poll 时自动索引，索引文件是 `data/file_index.json`，记发送者、文件名、保存路径、大小、日期和内容摘要，摘要规则是 PDF 前 3 页、XLSX 表名加前 5 行、DOCX 前 10 段、TXT 头 64KB，超过 20MB 不取摘要，未知格式没有摘要。查文件用 `ilink.py files [--limit N]` 和 `ilink.py files-find <关键词> [--limit N]`，每条结果印一行加下一行保存路径，`[已清理]` 表示文件已经不在磁盘上，搜不到打印 `FILES_NOT_FOUND` 并退出码 1。重发用 `ilink.py send-file <to> <path> [--key <id>]`。poll 还会清理索引里文件已经消失的行。

### 2. 新增 2.4 节：语音消息

位置：第 2 节末尾，`Hook 与 daemon 二选一` 那段之后，`## 3. Verify` 之前。新加 `## 2.4 语音消息`，三条：入站语音优先用微信自带的转写，自带转写缺失时用 `ilink.py transcribe` 本地转写，只打印一行结果，听不清就直说不猜。发语音走音频文件，先 `python3 tts_send.py "<text>" --out <path>.mp3` 再 `ilink.py send-file` 或 `proactive-file`，`send-voice` 和 `proactive-voice` 不要用。

### 3. 新增 2.5 节：提醒、待办与长任务进度

位置：2.4 节之后，`## 3. Verify` 之前。新加 `## 2.5 提醒、待办与长任务进度`，五条：待办用 `todos.py`，七个 todo 命令的用法和输出。时间含糊绝不猜，`todo-add` 报 `TODO_TIME_ERROR` 且不建待办。到期提醒走本运行时的 cron，runonce、北京时间、`flexible_time` 是 false、`timeout` 是 600、重试最多 1 次，任务体先 `proactive` 再 `todo-done`，只有 `PROACTIVE_OK` 或 `SEND_ALREADY_SENT` 才标记完成，建好后 `todo-set-cron` 回填任务 id，取消要 `todo-cancel` 加删 cron 两步。主动推送只有 24 小时窗口，窗口外不重推，过期提醒下次来消息时补送。长任务先回一句收条，中途最多一到两条纯文字进度，没有 `ilink.py progress` 这个命令。

## 依赖安装

- `voice_transcribe.py`：`ffmpeg` 和 `ffprobe` 放在 PATH 上。`faster-whisper` 放在独立 venv 里，默认 `~/.local/share/wechat-muse/whisper-venv/bin/python`，可用 `WHISPER_VENV_PYTHON` 环境变量覆盖。模型预先下载到 `~/.cache/huggingface`，例如用 venv 的 python 跑 `HF_HUB_OFFLINE=0 ~/.local/share/wechat-muse/whisper-venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"`，系统 python 没有装 `faster-whisper`，不要用它跑。
- `scripts/silk_decode.mjs`：需要 `node` 和 `silk-wasm` 这个 npm 包。装一次，例如 `mkdir -p ~/.local/share/wechat-muse/silk-wasm`，进去跑 `npm init -y` 再跑 `npm install silk-wasm`，也可用 `SILK_WASM_DIR` 环境变量指定位置。
- `tts_send.py`：`pip install gtts` 或 `pip install edge-tts`，两个后端都不需要 key。
- `file_index.py`：摘要提取按需用 `pypdf`、`openpyxl`、`python-docx`，没有这些库时对应格式返回空摘要，不报错。

## 接完验证

- 跑 `python3 -m unittest discover -s tests`，全量测试全部通过才算接线成功。
- 跑 `python3 ilink.py todo-list`，应打印 `TODO_EMPTY`。跑 `python3 ilink.py files`，应打印 `FILES_EMPTY`。
- 跑 `python3 ilink.py preflight`，应打印 `PREFLIGHT_OK`。
- 如果已经部署过桥，升级上游后按这份文档重新接一遍，不要直接覆盖 `ilink.py`。
