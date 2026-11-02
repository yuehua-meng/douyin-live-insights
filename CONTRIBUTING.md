# 参与贡献

感谢你愿意让直播内容更容易被整理和使用。

## 欢迎这些贡献

- 可复现的直播解析、音频处理或任务恢复问题。
- 提升商业案例质量、证据关联与原文回听体验。
- 部署教程、交互体验、可访问性与中英文文档。
- 在保持隐私边界的前提下改进测试和成本统计。

## 本地开发

Python 3.12+，安装 FFmpeg / ffprobe。测试不需要真实 DeepSeek Key，也不需要下载 ASR 模型：

```bash
python -m venv .venv
# Linux
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
# 标准库演示
python tools/demo.py
```

Windows 使用 `.\.venv\Scripts\python.exe`。实际录音需要 `requirements.txt` / `requirements-local.txt`；完整转写需要 `requirements-analysis.txt` 和模型。

## 提交方式

1. 先检查现有 Issue，说明问题和复现路径。
2. 从 `main` 创建分支，保持一个 PR 解决一个清晰问题。
3. 修复涉及逻辑时补充能复现问题的测试，界面改动附演示数据截图。
4. 说明改了什么、为什么改、怎样验证，以及需要注意的限制。

不要在 PR 中带入真实录音、转写内容、用户数据、服务器配置或密钥。不要为了让测试通过而关闭来源检查、登录认证或路径校验。未实现的能力在文档中明确标为计划，不能写成已经可用。

GitHub 是主要协作入口，Gitee 提供国内代码镜像。提交的贡献按项目 MIT 许可证分发；第三方代码须保留其许可信息。
