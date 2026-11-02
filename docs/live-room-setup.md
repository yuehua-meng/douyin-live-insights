# 如何配置自己的抖音直播间

[返回首页](../README.md)

## 先分清这三个东西

| 你拿到的内容 | 含义 | 怎么使用 |
| --- | --- | --- |
| 抖音号 / 昵称 | 账号标识 | 用于查找主播、核实身份，不负责直接解析直播流 |
| `https://v.douyin.com/.../` | App 分享短链接 | 先在浏览器打开，进入真正的直播间，再取最终地址 |
| `https://live.douyin.com/...` | 电脑端直播间入口 | 核实是目标主播后，填写 `room_url` |

**不能把任意抖音号拼到 `https://live.douyin.com/` 后面就当成直播间。** 有的房间路径可能是数字，有的可能是其他标识，以浏览器实际打开的地址和解析结果为准。

## 方法 A：从电脑端获取（推荐）

1. 在主播正在直播时，用电脑浏览器打开抖音。
2. 搜索并进入目标主播的直播间。核对昵称、头像以及正在播放的内容。
3. 查看地址栏，复制 `https://live.douyin.com/…` 这一类实际直播间 URL。
4. 把它填入 `config.toml` 的 `room_url`。不要填写账号主页、某条视频的链接或 CDN 播放地址。
5. 运行下面的 15 秒录制测试，试听声音是否来自目标直播间。

```text
找到主播 → 进入正在直播的房间 → 核实主播 → 复制地址栏 → 填入 room_url → 录 15 秒试听
```

## 方法 B：只有手机分享链接

1. 在抖音 App 的直播间点“分享”→“复制链接”。
2. 从分享文字中取出 HTTPS 链接，在电脑浏览器打开。
3. 如果跳转到落地页、App 下载页或主页，继续找到该主播正在直播的房间；**落地页本身不等于直播间地址**。
4. 获取电脑直播间的最终 URL，再按方法 A 核实。

当前配置校验不接受 `v.douyin.com` 短链。分享短链可能随操作变化，也可能跳转到带时效参数的页面；长期监测使用经过验证的电脑直播间入口。平台也可能改变入口，因此不能承诺一个 URL 永久有效。

> 主播没有开播时，可以先准备配置；但必须在实际开播期间做一次验证。没有可播放流时，程序会等待或报告离线，无法靠账号 ID 推测不存在的音轨。

## 配置示例

```toml
[target]
douyin_id = "my-streamer"
room_url = "https://live.douyin.com/这里替换成真实房间路径"

[paths]
recordings_dir = "./recordings"
work_dir = "./work"

[runtime]
poll_seconds = 60
segment_seconds = 1800
min_free_gb = 5
ffmpeg = "ffmpeg"
```

- `douyin_id`：本地归档目录的名称，建议使用简短字母、数字、短横线，例如 `my-streamer`。不是登录账号，也不是自动搜索指令。
- `room_url`：实际直播间地址，必须是 HTTPS。推荐使用 `live.douyin.com` 的电脑端 URL。
- `recordings_dir` / `work_dir`：分别保存已处理片段与录制临时文件。相对路径以启动程序的当前目录为基准。
- `ffmpeg`：Linux 通常填 `ffmpeg`；使用 Windows 便携依赖时填 `bundled`。完整 ASR 还需要可执行的 `ffprobe`，推荐在 Linux 部署分析工作台。

示例里的中文占位符只是说明，不能原样用于真实录制。仓库默认将 `room_url` 留空，防止误录某个示例账号。

## 验证与启动

Linux：

```bash
.venv/bin/python recorder.py --config config.toml --once --max-seconds 15
```

Windows：

```powershell
.\.venv\Scripts\python.exe recorder.py --config config.toml --once --max-seconds 15
```

你应检查：

- 输出来自你配置的主播，而不是同名账号。
- 文件可以播放，声音内容正确。
- 如果需要录连麦，验证样本里确实有连麦人发声。
- `recordings/<douyin_id>/日期/连接记录/` 中有 `session.json` 与有效的 `part_*.m4a`。静音或过短样本可能被隔离，需要换一段有声音的时间再试。

然后去掉 `--once --max-seconds 15`，开启持续监测；服务器上的 systemd 用法见 [部署指南](deployment.md)。

## 常见情况

| 现象 | 优先检查 |
| --- | --- |
| `room_url is not set` | 配置仍为空，或启动时用了错误的配置文件 |
| URL 校验失败 | 是否粘贴了整段分享文字、短链、HTTP 或错误域名 |
| `No playable streams found` / 离线 | 主播是否正在直播；浏览器是否能直接播放；链接是否仍指向目标房间 |
| 浏览器能看、服务器解析失败 | 云服务器网络、地区限制、平台风控及 Streamlink 插件兼容性 |
| 很多“连接记录” | 重连会新建记录，不代表主播多次开播；看连接结束原因，不只看日期 |
| 没有连麦人的声音 | 先确认观众端直播流是否包含这段声音；本程序不抓取独立麦位轨道 |

本项目通过 Streamlink 解析。若平台改变接口，应先检查 [Streamlink 抖音插件](https://github.com/streamlink/streamlink/blob/master/src/streamlink/plugins/douyin.py) 和 [插件支持说明](https://streamlink.github.io/plugins.html#douyin)。不要把 Cookie、带签名的播放 URL 或账号凭据发到公开 Issue。
