# 配置与费用

[返回首页](../README.md)

## 录音配置

录制器读取 `--config` 指定的 TOML；systemd 默认是 `/etc/douyin-recorder/config.toml`。修改后重启 `douyin-recorder`。字段说明和获取房间地址见 [直播间配置](live-room-setup.md)。

## 分析配置

完整安装器使用 `/etc/douyin-recorder/analysis/` 下的文件：

| 文件 | 作用 |
| --- | --- |
| `admin.env` | 网页管理员密码的 scrypt 校验值 |
| `deepseek.env` | `DEEPSEEK_API_KEY` |
| `common.env` | 网站来源、模型与费用估算等 |
| `automation.env` | 日批处理与推送时间 |
| `wecom.env` | 可选的群 Webhook 或 Bot ID / Secret |

`common.env` 常用变量：

| 变量 | 默认值 / 说明 |
| --- | --- |
| `ADMIN_ORIGIN` | 由安装向导填写，如 `https://live.example.com`；不含页面路径 |
| `ANALYSIS_DATA` | `/srv/douyin-recorder/analysis` |
| `RECORDINGS_DIR` | `/srv/douyin-recorder/recordings`；应与录制器一致 |
| `DEEPSEEK_MODEL` | `deepseek-flash`，使用当前账号可调用的模型 |
| `DAILY_BUDGET_MICRO_USD` | `1000000` = 估算 1 美元 / 天 |
| `TASK_BUDGET_MICRO_USD` | `200000` = 估算 0.2 美元 / 任务 |
| `INPUT_USD_PER_MILLION` | `0.3`，每百万输入 token 的估算美元单价 |
| `OUTPUT_USD_PER_MILLION` | `1.2`，每百万输出 token 的估算美元单价 |

程序请求 DeepSeek 官方 API。价格、模型可用性、缓存计费可能变化，请根据 [DeepSeek 官方模型与价格说明](https://api-docs.deepseek.com/quick_start/pricing/) 更新估算参数。应用没有替你创建或充值 API 账户。

修改通用配置后，重启相关进程使其读取新值：

```bash
sudo systemctl restart douyin-analysis-api douyin-analysis-asr \
  douyin-analysis-summary douyin-analysis-scheduler
```

若已启用智能机器人，也重启 `douyin-wecom-bot`。变更配置前注意正在执行的任务；状态和检查点会保存，但重启不是无成本的“撤销 API 请求”。

## 为什么分析可能比一次聊天花费多

完整分析可能包含多个阶段：文字检查、证据提取、项目归并、总结、逐项目口播、结果修复。项目多、原文长、重新生成和改变提示词版本都会影响请求量。并非“一段 30 分钟音频只发一次请求”。

降低费用的实用方式：

1. 先用少量有效录音确认内容质量，再处理大量历史。
2. 复用已有转写和分析缓存，不反复提交相同素材。
3. 保留音频与文字质量过滤，避免把静音、口头语送入完整分析。
4. 查看任务状态和预估费用，再决定是否提高预算。

预算检查可能按输入与最大输出保守预留，并不等于最终消费。网页统计不是 DeepSeek 账单，实际扣费以服务商为准。达到预算时任务进入待处理状态，不应通过重复新建任务绕过限制。

## 自动处理时间

```ini
DAILY_BATCH_MODE=1
DAILY_PROCESS_HOUR=9
DAILY_SEND_HOUR=11
WECOM_DIGEST_ENABLED=0
```

时间按 `Asia/Shanghai` / 北京时间计算，处理小时必须早于发送小时。关闭日报推送不会删除成果；暂停自动处理不等于取消已入队任务。日批建立快照后到来的新录音通常进入次日快照。

## 保管数据

录音、转写、SQLite 数据库、模型权重、真实配置均不应提交到 Git。本仓库 `.gitignore` 默认排除这些内容。DeepSeek 总结阶段会把相应转写文字发送给 DeepSeek；本地 ASR 不需要上传原始音频到付费转写服务。请确认你有权处理与分享这些素材。
