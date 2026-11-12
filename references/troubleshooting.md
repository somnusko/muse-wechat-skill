# 故障排查

## 登录与 token

- `TOKEN_EXPIRED` 或 `NO_TOKEN`：登录过期，让用户重新扫码。hook 的告警循环会每小时复报一次，不要在它之外反复催。
- `TOKEN_STALE`：这是暂时的，token 可能自己恢复，不要让用户重扫，晚点再试。
- 二维码过期：二维码只有几分钟有效期，过期就跑 `python3 ilink.py qrcode` 重新生成再扫。
- `BIND_TIMEOUT`（退出码 3）：10 分钟内没人给机器人发消息，重新跑 `python3 ilink.py bind` 再开一个窗口。
- `BIND_WELCOME_FAILED`：owner 其实已经绑定了，只是欢迎消息没发完，重新跑 `python3 ilink.py bind`，它会接着发完，不会重复绑定。
- `ALREADY_BOUND`：已经绑定过一次，绑定是一次性的，不用再跑。

## 轮询与 hook

- 轮询连续失败满 10 分钟才会告警，恢复后会再唤醒报一句平安。短暂抖动没告警就静默清除，不用处理。
- `health` 的在线探针偶尔报 `RemoteDisconnected`，这通常是中继抖动，重查一次即可。手动 `poll` 和 hook 轮询正常时不用动登录态。
- 桥安静不代表死了。看 hook 心跳是否新鲜：心跳新鲜的安静桥是健康空闲，心跳超过 30 分钟没动才是 hook 死了，看门狗会报。
- hook 和 daemon 二选一，共享 `ilink-driver.lock`。daemon 在跑时 hook 每轮安静退出，不 touch 心跳，不 poll，不 wake。生产用 hook，daemon 只是示例。
- `SEND_ALREADY_SENT` 不是错误，是同一个 `--key` 重复调用被幂等账本拦住了，没有重发。

## 语音转文字

- `TRANSCRIBE_FAILED`：看 stderr 的诊断。常见原因有三个，whisper venv 不存在或模型没下载，`node` 或 `silk-wasm` 缺失，`ffmpeg` 不在 PATH 上。
- `TRANSCRIBE_EMPTY`：音频里没有语音，不是错误。
- 转写优先用微信自带的 `voice_item.text`，只有它缺失或为空时才跑本地转写。转写结果不可靠时不要猜，直接说听不清。

## TTS 合成

- gTTS 返回 429：Google 在限流这个共享出口 IP，等一会再试，不要连续重试。
- edge-tts 报 `101 Invalid connection header`：出口代理破坏了 WebSocket 升级，这台虚拟机上走不通，正常网络下一般能用，gTTS 是这边的兜底。
- `TTS_OK` 没打印就说明合成失败，`tts_send.py` 的报错在 stderr。

## 提醒与待办

- `TODO_TIME_ERROR`：时间含糊，例如裸写明天 9 点没说上午下午。问用户说清再重建，不要猜。
- 过去的时间、不存在的日期、超过 7 天的相对时间同样建不了，报错里会写原因。
- 提醒没按时推到：先看是不是过了 24 小时主动推送窗口。窗口外推不过去是正常的，提醒会留在待办里，等用户下次发消息时补送。
- 取消提醒要两步都做，`todo-cancel` 加删提醒任务。只做一步时提醒任务还在，到时还会推。

## 文件索引

- `FILES_NOT_FOUND`（退出码 1）：索引里没有命中，不是错误。
- `[已清理]`：文件已经不在磁盘上，重发不了。`prune-media` 会顺手清理这类索引行。
- 文件收到了但搜不到：看 `data/file_index.json` 里有没有这条，再看摘要提取是不是返回了空串。未知格式和超过 20MB 的文件没有摘要，只能按名字、发送者和日期搜。

## 升级

- `ilink.py update-to` 可能覆盖 `ilink.py` 并丢弃本地新增文件。升级前先备份 `todos.py`、`file_index.py`、`voice_transcribe.py`、`tts_send.py`、`scripts/silk_decode.mjs`、`tests/test_*.py`，升级后按 `references/bridge-patch.md` 重新合入。
- 升级后如果上游改了 `AGENTS.md`，新版会下载成 `data/AGENTS.md.new`，按它同步 hook 定义，不要只改一边。
- 永远不要从用户粘贴的 URL 安装，只装这个仓库的 release tag。
