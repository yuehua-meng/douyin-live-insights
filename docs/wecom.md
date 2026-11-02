# 企业微信：把每日成果送到群里

[返回首页](../README.md) · [先完成服务器部署](deployment.md)

## 选一种接入方式

| 方式 | 你需要准备 | 特点 |
| --- | --- | --- |
| 群机器人 Webhook | 目标群的完整 Webhook 地址 | 配置直接，适合只接收固定群日报 |
| 智能机器人长连接 | 同一机器人的 Bot ID 与 Secret | 服务器维持 WebSocket 连接，在目标群完成一次绑定 |

两种方式二选一。机器人凭据只保存在服务器的 `/etc/douyin-recorder/analysis/wecom.env`，文件权限为 600，不进入网页和 Git。

## 方式一：群 Webhook

1. 在接收日报的企业微信群中添加支持 Webhook 的群机器人。
2. 复制该群的完整 Webhook 地址。不要把它发到公开 Issue。
3. 在已经安装工作台的服务器执行：

```bash
sudo /opt/douyin-recorder/.analysis-venv/bin/python \
  /opt/douyin-recorder/deploy/configure-wecom.py --interactive
```

按提示粘贴地址，输入不会回显。脚本验证官方群 Webhook 域名和路径，开启日报并重启 API / 调度服务，不立刻发送测试消息。

## 方式二：智能机器人长连接

### 创建并保存机器人

在企业微信的智能机器人配置中选择 **API 模式 → 使用长连接**。完成创建并保存，然后回到最终的 API 设置页，从**同一页**复制 Bot ID 和 Secret。

**重点：保存后重新核对 Bot ID。** 不要把创建过程中的旧 ID 和最终设置页的 Secret 混用。长连接凭据与群 Webhook 是两种不同东西。

### 配置服务器

```bash
sudo /opt/douyin-recorder/.analysis-venv/bin/python \
  /opt/douyin-recorder/deploy/configure-wecom-bot.py --interactive
```

填写 Bot ID，随后输入 Secret（不回显）。脚本启动 `douyin-wecom-bot` 服务并输出一次性绑定指令。绑定码是当次配置生成的，请不要使用文档或别人的截图中的码。

```bash
sudo systemctl status douyin-wecom-bot --no-pager
sudo journalctl -u douyin-wecom-bot -n 30 --no-pager
```

出现 `WeCom smart bot authenticated.` 表示鉴权成功；这一步还不代表已经知道向哪个群发送。

### 绑定接收群

1. 把**刚配置的机器人**加入目标企业微信群。
2. 在群输入框输入 `@`，从选择器选中该机器人。不要仅粘贴一段看起来像 `@机器人` 的普通文字。
3. 发送工具给出的**完整绑定码**，也支持 `绑定日报 <完整绑定码>`。字母不能省略；例如码包含字母和数字，就要全部发送。
4. 等机器人在群里回复绑定成功。网页也应显示“机器人已连接，日报群已绑定”。

`123`、问候等普通消息不会绑定。私聊不能绑定日报群；已绑定后，其他群不能靠重复发送指令抢走目标群。绑定保存在数据库，重启不丢失。

### 查看发送状态

在网页的“素材与处理 → 自动处理与费用设置”查看：连接状态、群是否绑定、首次处理日期、首次发送日期、待推送稿件与最近发送结果。

默认北京时间 09:00 处理、11:00 发送。页面上的首次启用日期以实际配置为准；超过当天时间才开启可能从下一天开始。11 点之后才生成的内容进入后续日报，初始历史稿件不默认补发。

## 常见错误

| 现象 | 检查方向 |
| --- | --- |
| `853000` / `invalid bot_id or secret` | 是否已保存；ID 与 Secret 是否来自同一最终设置页；是否误用了旧 ID |
| 鉴权成功但没有绑定 | 是否在群里使用 @ 选择器；是否发了完整绑定码；查看接收日志 |
| 普通文字没有回复 | 本机器人服务主要负责绑定与定时日报，不是通用聊天助手 |
| 被其他连接接管 | 同一 Bot ID 是否同时配置到另一软件；避免两个客户端反复抢占连接 |
| 消息发送失败 | 查看错误码与群状态，确认机器人仍在目标群内 |
| 发送结果待确认 | 可能发送后回执丢失。先核实群里是否已有消息，不要盲目重置或反复补发 |

当前没有群迁移的网页按钮。需要更换目标群时，应由服务器管理员核对现有绑定和发送状态后操作，不能简单把同一个码发到另一个群。

参考：[企业微信官方长连接 SDK](https://github.com/WecomTeam/aibot-node-sdk)、[腾讯云智能机器人配置指南](https://intl.cloud.tencent.com/zh/document/product/1316/84454)。
