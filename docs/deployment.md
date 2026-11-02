# 部署指南

[返回首页](../README.md) · [先配置直播间](live-room-setup.md)

## 选一种开始方式

| 目标 | 方式 |
| --- | --- |
| 先看看界面 | `python tools/demo.py`，无需额外依赖和密钥，虚构数据演示 |
| 本机试录一段 | 按首页 Windows / Linux 步骤运行 `recorder.py` |
| 24 小时录音 | Ubuntu 24.04 + `douyin-recorder.service` |
| 完整转写、商业分析、口播、日报 | 在录音服务基础上增加分析工作台与企业微信配置 |

建议使用 Ubuntu 24.04、Python 3.12、至少 4 GB 内存；更长素材和并发任务建议更多内存。预留模型、录音和临时文件空间；默认磁盘保护阈值是 5 GB。模型下载量和处理速度受网络、CPU 和素材影响，不承诺固定完成时间。

公开版安装脚本面向新部署环境；升级已有安装前请备份 `/etc/douyin-recorder` 和 `/srv/douyin-recorder`，并审阅配置变化。

## 1. 获取代码，安装录音服务

```bash
git clone https://github.com/yuehua-meng/douyin-live-insights.git
cd douyin-live-insights
sudo bash deploy/install-ubuntu.sh
sudo nano /etc/douyin-recorder/config.toml
```

按 [直播间配置](live-room-setup.md) 填入真实 `room_url`。安装器安装 FFmpeg、Python 环境和 systemd 单元，设置开机启动，但不会立即启动录制。

主播开播时先测试：

```bash
sudo -u douyinrec /opt/douyin-recorder/.venv/bin/python \
  /opt/douyin-recorder/recorder.py \
  --config /etc/douyin-recorder/config.toml --once --max-seconds 15
```

试听确认之后：

```bash
sudo systemctl start douyin-recorder
sudo systemctl status douyin-recorder --no-pager
```

## 2. 安装完整工作台

仍在克隆下来的项目目录执行，**不要在 `/opt/douyin-recorder` 内运行安装器**：

```bash
sudo bash deploy/install-dashboard.sh
sudo /opt/douyin-recorder/.analysis-venv/bin/python \
  /opt/douyin-recorder/deploy/configure-dashboard.py \
  --origin https://live.example.com
```

把 `https://live.example.com` 换成你的实际 HTTPS 域名。脚本会交互式要求设置网页管理密码和 DeepSeek Key，不在屏幕上显示输入值。Key 可以先留空；未配置时不能调用 DeepSeek，但不影响已有转写保存。配置已存在时脚本拒绝覆盖，避免意外重置。

管理密码至少 12 个字符。原始密码不存入配置，服务只保存 scrypt 校验值。`ADMIN_ORIGIN` 必须与浏览器地址的协议、主机和端口完全一致，不能带 `/recorder-admin/` 路径。

默认公开版设置每日估算预算 1 美元、单任务 0.2 美元，可以在服务器配置中调整。长录音或多项目分析可能达到预算，需要查看任务状态并由你决定是否上调。详见 [配置参考](configuration.md)。

## 3. 下载开源 ASR 模型

```bash
sudo -u douyinanalysis /opt/douyin-recorder/.analysis-venv/bin/python \
  /opt/douyin-recorder/deploy/download_models.py
```

脚本从 ModelScope 下载 Paraformer、FSMN VAD、标点恢复的 ONNX 文件，放入 `/srv/douyin-recorder/analysis/models/`，并记录文件哈希。请先阅读模型仓库的许可证。不要把下载的模型权重提交到 Git。

## 4. 配置 HTTPS 入口

把自己的域名 A 记录指向服务器，开放所需的 80/443 入站端口。分析 API 只监听 `127.0.0.1:8790`，通过反向代理对外提供 `/recorder-admin/`。

可以使用 Caddy，也可以使用已有的 Nginx。Caddy 示例见 [`deploy/Caddyfile.example`](../deploy/Caddyfile.example)。若是全新服务器：

```bash
sudo apt-get install -y caddy
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-douyin
sudo nano /etc/caddy/Caddyfile
```

填写示例配置并替换域名；已有其他站点时添加独立站点块，不覆盖其他配置。然后：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy 可为正确解析并可达的域名办理 HTTPS 证书；网络、DNS 与证书问题可参考 [Caddy 文档](https://caddyserver.com/docs/automatic-https)。不建议为公网部署关闭认证或 Secure Cookie。

## 5. 启动后台服务

```bash
sudo systemctl enable --now douyin-analysis-api douyin-analysis-asr \
  douyin-analysis-summary douyin-analysis-scheduler
sudo systemctl is-active douyin-recorder douyin-analysis-api \
  douyin-analysis-asr douyin-analysis-summary douyin-analysis-scheduler
```

打开 `https://你的域名/recorder-admin/`，使用自己设置的管理密码登录。录音、口播列表、商业总结统一在这里；`/recorder/` 不是本公开版的入口。

刚安装时列表为空是正常的，需要先有有效录音，再完成转写和分析。系统默认按北京时间日批处理，首次启用的处理与发送日期可在网页“素材与处理 → 自动处理与费用设置”查看。要立即试用一段素材，可以在“素材与处理”中选择录音，手动提交“补转文字并总结”。这会按配置调用模型并计费。

## 6. 企业微信日报（可选）

按 [企业微信接入](wecom.md) 选择 Webhook 或智能机器人。没有配置群推送不影响录音和内容生成。默认不会把初始历史稿件全部补发到群里。

## 日常维护

```bash
# 查看错误与任务状态；分享日志前先脱敏
sudo journalctl -u douyin-recorder -n 80 --no-pager
sudo journalctl -u douyin-analysis-asr -n 80 --no-pager
sudo journalctl -u douyin-analysis-summary -n 80 --no-pager
sudo journalctl -u douyin-analysis-scheduler -n 80 --no-pager

# 录音与状态存储
du -sh /srv/douyin-recorder/recordings /srv/douyin-recorder/analysis
df -h /srv/douyin-recorder
```

备份包括录音、分析数据库及配置文件。在线复制 SQLite 时使用数据库备份接口或停服务后复制，不要只复制一个正在写入的 `.sqlite3` 而遗漏 WAL。密钥配置备份应限制访问。项目不会自动清空历史录音，请自行制定归档和磁盘保留策略。
