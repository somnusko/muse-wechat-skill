# overlay：本地移植功能的源码

这里的文件是微信桥的本地移植实现，全部从已部署的桥目录原样复制，未改一字节。它们不在上游仓库里，升级上游时会丢，升级前先备份这里。

## 怎么用

把本目录里的文件复制进桥目录的对应位置，覆盖同名文件：

```
overlay/voice_transcribe.py      -> <桥目录>/voice_transcribe.py
overlay/tts_send.py              -> <桥目录>/tts_send.py
overlay/file_index.py            -> <桥目录>/file_index.py
overlay/todos.py                 -> <桥目录>/todos.py
overlay/scripts/silk_decode.mjs  -> <桥目录>/scripts/silk_decode.mjs
overlay/tests/test_file_index.py -> <桥目录>/tests/test_file_index.py
overlay/tests/test_todos.py      -> <桥目录>/tests/test_todos.py
```

复制完再按 `../references/bridge-patch.md` 接好 `ilink.py` 的分发，`AGENTS.md` 的新段落按同一份文档手工合入。只复制文件不接分发时，新命令跑不起来。

## 文件说明

- `voice_transcribe.py`：入站语音的本地转写兜底。优先用微信自带的转写，缺失时才跑它。SILK 先由 `scripts/silk_decode.mjs` 解成 PCM，再由 `ffmpeg` 转成 16k 单声道 WAV，最后由 `faster-whisper` 转写。默认 tiny 模型，`--model base` 换大模型。
- `tts_send.py`：语音文件替代的合成器。`python3 tts_send.py "<text>" --out <path>.mp3`，成功打印 `TTS_OK:<path>:<bytes>:<duration_ms>`。默认 gTTS，`--backend edge` 换微软音色。
- `file_index.py`：收到的文件索引。poll 时自动记一条，`ilink.py files` 列最近文件，`ilink.py files-find <关键词>` 按名字、发送者、日期或内容词搜，`ilink.py send-file` 重发。
- `todos.py`：提醒与待办的记账本。`ilink.py todo-add`、`todo-list`、`todo-done`、`todo-show`、`todo-set-cron`、`todo-cancel`、`todo-cleanup` 都分发到这里。时间含糊时报错不建，绝不猜。
- `scripts/silk_decode.mjs`：SILK 解码小工具，`node silk_decode.mjs <input.silk> <output.pcm>`。需要 `silk-wasm` 这个 npm 包。
- `tests/test_file_index.py`：文件索引的测试，含摘要提取、并发写盘、清理一致性和命令行入口。
- `tests/test_todos.py`：待办的测试，含时间解析、记账流程和命令行入口。

## 依赖

- `voice_transcribe.py` 需要 `ffmpeg` 和 `ffprobe` 在 PATH 上，`faster-whisper` 放在独立 venv 里（默认 `~/.local/share/wechat-muse/whisper-venv/bin/python`，可用 `WHISPER_VENV_PYTHON` 覆盖），模型预先下载到 `~/.cache/huggingface`。
- `scripts/silk_decode.mjs` 需要 `node` 和 `silk-wasm`。装一次即可，例如 `mkdir -p ~/.local/share/wechat-muse/silk-wasm`，进去 `npm init -y` 再 `npm install silk-wasm`，也可用 `SILK_WASM_DIR` 指定位置。
- `tts_send.py` 需要 `pip install gtts` 或 `pip install edge-tts`，两个后端都不需要 key。
- `file_index.py` 的摘要提取按需用 `pypdf`、`openpyxl`、`python-docx`，没有这些库时对应格式返回空摘要，不报错。
