# 微信接 Muse

## 部署桥

- 从 https://github.com/gfydfhcf/wechat-ilink-muse-bridge 克隆上游仓库，这个接法就是它的 Muse hook 路线，上游用 MIT 许可。

- 桥里的 iLink 客户端协议对照腾讯官方插件的公开源码还原，协议来源是 https://github.com/Tencent/openclaw-weixin。

- 在仓库目录跑 `pip install -r requirements.txt`，Python 需要 3.10 以上，硬依赖只有 `cryptography`。

- 跑 `python3 ilink.py qrcode` 拿二维码图片，再跑 `python3 ilink.py login <qrcode-id>`，等用户在微信里扫码确认，二维码只有几分钟有效期，过期就重新生成。

- 登录成功后跑 `python3 ilink.py bind`，它打开一个 600 秒的绑定窗口，用户在 10 分钟内给机器人发任意一条微信消息就完成绑定，绑定完成前不要启动 hook 或 daemon。

- 按 `references/bridge-patch.md` 把 `overlay/` 里的文件复制进桥目录，再接好 `ilink.py` 的分发，`AGENTS.md` 的新段落按同一份文档手工合入。

- 读 `examples/muse-hook/README.md`，把 `examples/muse-hook/ilink-watch.sh` 接成 Muse hook，每 5 秒跑一轮，再配一个每 15 分钟跑一次的看门狗，跑 `python3 ilink.py watchdog` 做心跳检查。

- 语音转写还要装本地依赖，`faster-whisper` 放在独立 venv 里，`node` 加 `silk-wasm` 负责解 SILK，`ffmpeg` 和 `ffprobe` 要在 PATH 上，细节见 `references/bridge-patch.md`。

- 下面六项功能是本地移植实现，点子来自 https://github.com/6688dong/wechat-muse-bridge-public，只参考了点子，没有复制它的代码，它的许可是 PolyForm Noncommercial。

## worker 契约要点

- 先用 `mv` 把待处理文件原子认领成处理中文件，认领后立刻 `touch` 一次，处理期间大约每 60 秒再 `touch` 一次续租，10 分钟没动过的认领会被回收重唤。

- 一批消息只回一次，发送前再看一眼待处理队列，有新消息就合并进来重新组织，最多合三轮。

- 回复用 `ilink.py send <to> <ctx> - --key <message_id>`，正文走 stdin，`--key` 用本批首条消息的 id，多气泡时每个气泡的 key 加 `-1` 和 `-2` 后缀，重复调用同一 key 只回 `SEND_ALREADY_SENT`。

- 先处理特殊意图再写正常回复，精确的 `订阅` 或 `订阅更新` 走订阅，精确的 `取消订阅` 走退订，24 小时内的 `subscribe_ask.json` 和 `update_offer.json` 标记按肯定或否定分支处理，`安装更新` 这个关键词没有标记时也一样生效。

- 绝不在微信里批准新发送者，被拒的 id 会进 `data/unknown_senders.json`，只在主对话里由用户确认后批准。

- 机器人只能收到发给它的消息，读不到用户主账号的聊天、通讯录或朋友圈，不要承诺这些。

- 回复可用 Markdown，不要用任务列表、图片语法或 `$...$` 公式，图片用 `send-image` 发。

- 写回复超过 30 秒就刷新一次正在输入指示，发完停掉，回复后跑 `ilink-warm-wait.sh` 守候跟进，空闲 180 秒或总共 30 分钟后安静退出。

- 每轮处理完往 `data/chatlog.md` 记一行，完整契约见 `references/worker-contract.md`。

## 语音转文字

- 用户在微信里直接发语音，语音条目自带 `voice_item.text` 时直接拿它当正文，不要再转一遍。

- 自带转写缺失或为空时跑 `ilink.py transcribe <media_path>`，默认用 tiny 模型，`--model base` 换大模型，`--timeout` 默认 120 秒。

- 命令只打印一行结果，`TRANSCRIBE_OK:<text>` 是转写成功，`TRANSCRIBE_EMPTY` 是音频里没有语音，`TRANSCRIBE_FAILED:<reason>` 是转不了，听不清就直说听不清，不要猜一句像样的话顶上去。

- 转写走本地 `faster-whisper`，SILK 文件先由 `scripts/silk_decode.mjs` 解成 PCM 再转，依赖见部署一节。

## 文件索引与找回

- 每次 poll 收到文件都会自动记进 `data/file_index.json`，记发送者、文件名、保存路径、大小、日期和一段内容摘要。

- 跑 `ilink.py files [--limit N]` 列最近收到的文件，跑 `ilink.py files-find <关键词> [--limit N]` 按名字、发送者、日期或内容词搜，搜不到打印 `FILES_NOT_FOUND` 并退出码 1。

- 找到文件后拿清单里印的保存路径跑 `ilink.py send-file <to> <path>` 重发，`[已清理]` 表示文件已经不在磁盘上，重发不了。

- 摘要按格式取，PDF 前 3 页，XLSX 取表名加首张表前 5 行，DOCX 取前 10 段，纯文本取文件头，超过 20MB 的文件不取摘要，未知格式没有摘要。

## 提醒与待办

- 用户在微信里说要提醒时跑 `ilink.py todo-add <due> <text>`，`due` 写 `-` 表示没有到期时间，否则写能解析的时间，例如 `10 分钟后` 或 `明天早上 8 点` 或 `2026-11-13 08:00`。

- 建成功打印 `TODO_OK:<id>`，时间含糊时打印 `TODO_TIME_ERROR:<原因>` 并且不建待办，问用户说清上午还是下午再重建，不要猜。

- 跑 `ilink.py todo-list` 列未完成待办，`--all` 也列已完成的，跑 `ilink.py todo-show <id>` 看一条的完整记录，里面有提醒任务的 id。

- 有到期时间的待办用 `cron.add` 建一个 runonce 提醒任务，`at` 写准确的北京时间，`flexible_time` 写 false，`timeout` 写 600，重试最多 1 次，任务体先跑 `ilink.py proactive <owner_id> <提醒文案> --key remind-<id>`，只有打印了 `PROACTIVE_OK` 或 `SEND_ALREADY_SENT` 才接着跑 `ilink.py todo-done <id>`，建好后立刻跑 `ilink.py todo-set-cron <id> <cron_id>` 回填任务 id。

- 取消提醒要两步都做，先跑 `ilink.py todo-cancel <id>`，再用 `cron.remove` 删掉对应的提醒任务，只做一步不算取消干净。

- 主动推送只有 24 小时窗口，用户超过 24 小时没发消息就推不过去，不要反复重推，提醒留在待办里，等用户下次发消息时连同正常回复一起补送，补送成功再标记完成。

## 长任务进度

- 接到长任务先回一句收条，例如收到正在处理，执行中途最多再发一到两条纯文字进度。

- 进度就是普通的 `ilink.py send`，每条气泡的 key 在原消息 id 后加 `-1` 和 `-2` 后缀区分，没有 `ilink.py progress` 这个命令，也不要用工具进度消息，服务端不支持那类消息。

## 语音文件替代

- 真语音气泡发不出去，服务端收到后静默丢弃，`send-voice` 和 `proactive-voice` 已经实现但同样被丢弃，不要用。

- 想出声时先合成音频文件，跑 `python3 tts_send.py "<text>" --out <path>.mp3`，成功打印 `TTS_OK:<path>:<bytes>:<duration_ms>`。

- 再用 `ilink.py send-file <to> <path>.mp3` 发出去，主动推送用 `proactive-file`，发完告诉用户这是一段音频文件，不是语音气泡。

- 默认后端是 gTTS，共享出口 IP 上可能遇到 Google 的 429 限流，遇到时等限流过去再试，`--backend edge` 加 `--voice zh-CN-YunyangNeural` 可以换微软的音色，但 edge 后端在有出口代理破坏 WebSocket 升级的环境里走不通。

## 文件处理

- 图片和文件条目都带下载好的 `media_path`，直接打开读内容再回答，不要只凭文件名猜。

- 只有 `media_error` 或格式确实读不了时才说打不开，视频内容读不到。

- 大文件先回一句小收条再抽样读，回复里至少引用一个只有实读才知道的细节，例如数量、第一项或一个数字。

- 摘要提取规则见文件索引一节，超过 20MB 的文件没有摘要。

## 验证

- 装完先跑 `python3 ilink.py preflight`，看到 `PREFLIGHT_OK` 再继续。

- 跑 `python3 ilink.py health`，登录有效且 token 有效时判 OK，`--offline` 跳过在线探针。

- 手动跑一次 `python3 ilink.py poll`，空批次加新游标说明收消息的路是通的。

- 跑 `python3 -m unittest discover -s tests`，全量测试全部通过才算接线成功。

- 让用户在微信里给机器人发一条消息，确认收到且只回一条，正在输入指示及时出现。

- `python3 ilink.py smoke-test --no-send` 只做本地检查不发真消息，不带 `--no-send` 会给 owner 发一条真文本加一张 1x1 测试图。

- 微信侧的五项真机检查当时还没跑完，真语音转写、TTS 音频播放、提醒闭环、文件找回重发、长任务进度气泡都要在真微信上逐项试过才算完成。

## 限制与风险

- 真语音气泡发不出去，只能发音频文件，见语音文件替代一节。

- 主动推送只有 24 小时窗口，窗口外不要反复重推。

- 二维码只有几分钟有效期，过期就重新生成再扫。

- `data/session.json` 是登录凭证，只放本地，不要提交，不要发送，不要写进任何文档。

- 升级前先备份本地新增文件，`ilink.py update-to` 可能覆盖 `ilink.py` 并丢弃它们，升级后按 `references/bridge-patch.md` 重新合入。

- 登录过期时 `ilink.py health` 会报 `TOKEN_EXPIRED`，让用户重新扫码，`TOKEN_STALE` 是暂时的，不要让用户重扫。

- 轮询探针偶尔会报 `RemoteDisconnected`，这通常是中继抖动，重查一次即可。

## Available connectors

- Use `muse.connector_search` to find other connectors and `muse.skill_search` to find related skills.

- If nothing here fits, look on the web for a maintained API or MCP server and set it up with [Custom Connectors and MCPs](/opt/muse/skills/custom-connector-setup/SKILL.md), or build a custom connector.

## Websites

- [wechat-ilink-muse-bridge](https://github.com/gfydfhcf/wechat-ilink-muse-bridge) for the bridge source and its releases, check the release tag before running `update-to` and read the release notes for the `ilink.py sha256` line.

- [openclaw-weixin](https://github.com/Tencent/openclaw-weixin) for the official Tencent plugin source the bridge protocol follows, read it when the protocol changes.

## Alternatives for better results

- 如果 Muse hook 运行时不可用，用仓库自带的 `daemon.py` 常驻循环代替，`daemon.py --llm` 接 OpenAI 兼容 API，不是 Muse，只适合不需要 Muse 记忆的轻量机器人。

- 如果用户只要一个临时测试机器人，`daemon.py --echo` 可以先验证收发循环，不用先接 hook。
