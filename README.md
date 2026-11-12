# 微信接 Muse

把个人微信接到 Muse 上，让用户在微信里直接指挥这个有记忆的个人助理。部署一次，微信里的机器人就是 Muse 本人，带着同样的记忆和上下文。

## 致谢

这个 skill 站在三个开源项目上面，没有它们就没有这个接法。

- 首要上游：[gfydfhcf/wechat-ilink-muse-bridge](https://github.com/gfydfhcf/wechat-ilink-muse-bridge)。这个 skill 部署的桥就是它的 Muse hook 路线，MIT 许可。没有它就没有这个接法。
- 协议来源：[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)。腾讯官方的 OpenClaw 微信插件，桥里的 iLink 客户端协议就是对照它的公开源码还原的。
- 功能点子来源：[6688dong/wechat-muse-bridge-public](https://github.com/6688dong/wechat-muse-bridge-public)。只参考了它的功能点子，包括语音转文字、文件索引、提醒待办和长任务进度，没有复制它的代码。它的许可是 PolyForm Noncommercial。

## 快速开始

1. 把本仓库克隆到 `~/workspace/custom-skills/muse-wechat-skill/`，本地安装就完成了。
2. 按 `SKILL.md` 的部署一节走：克隆上游桥，装依赖，扫码登录，10 分钟内绑定 owner，启用 hook 和看门狗。
3. 按 `references/bridge-patch.md` 把 `overlay/` 接进桥，跑全量测试。
4. 让用户在微信里给机器人发一条消息，确认收到且只回一条。

## 目录说明

```
SKILL.md                  方法全文：部署、worker 契约、六项功能、验证、限制与风险
skill.yaml                skill 描述：名字、标题、分类
overlay/                  本地移植功能的源码，原样复制进桥目录
  voice_transcribe.py     入站语音的本地转写兜底
  tts_send.py             语音文件替代的合成器
  file_index.py           收到的文件索引
  todos.py                提醒与待办的记账本
  scripts/silk_decode.mjs SILK 解码小工具
  tests/                  移植功能的测试
references/
  bridge-patch.md         把 overlay 接进上游 ilink.py 和 AGENTS.md 的精确说明
  worker-contract.md      hook prompt 全文，一字未改
  troubleshooting.md      故障排查
README.md                 本文件
LICENSE                   MIT
```

## 能做什么

微信里直接指挥 Muse，带着记忆和上下文。六项功能：

- 语音转文字：发语音条，优先用微信自带转写，缺失时本地 faster-whisper 转写。
- 文件索引与找回：收到的文件自动索引，按名字、发送者、日期或内容词找回重发。
- 提醒与待办：微信里说一声就建提醒，到时在微信上推过来，时间含糊时问清再建，绝不猜。
- 长任务进度：先回一句收条，中途最多一到两条纯文字进度。
- 语音文件替代：真语音气泡发不出去，改发 TTS 合成的 MP3 文件卡片。
- 文件处理：PDF、Excel、Word 发过来直接读内容回答，不只是存下来。

多账号不在范围里。真语音气泡发不出去是 iLink 服务端的限制，不是这个 skill 能绕开的。

## 验证状态

- 桥部署与全量测试已验证通过，`preflight` 和 `health` 正常，手动 `poll` 收消息的路是通的。
- 微信侧的五项真机检查在发布时还没跑完：真语音转写、TTS 音频播放、提醒闭环、文件找回重发、长任务进度气泡。逐项试过才算完成。

## 许可

本仓库用 MIT 许可，见 `LICENSE`。上游桥同样是 MIT 许可。功能点子来源的仓库用 PolyForm Noncommercial 许可，只参考了点子，没有复制代码，见上面致谢。
