# 贡献指南 / Contributing

[中文](#中文) | [English](#english)

## 中文

感谢你愿意改进 ComfyBridge！提交 Issue 或 Pull Request 前，请先阅读本指南。安全问题请勿公开提交，见 [安全策略](<SECURITY.md>)。

### 开发环境

需要 Windows 10／11 x64、[uv](https://github.com/astral-sh/uv) 和 Python 3.13（可由 uv 自动安装）。

```powershell
uv sync --frozen --python 3.13
.venv\Scripts\python.exe -m pytest -q
```

可选：获取固定版本的 FFmpeg／ffprobe，并把其 `bin` 目录加入当前会话的 `PATH`，使依赖 FFmpeg 的测试也能运行（否则这些测试会被跳过）：

```powershell
pwsh -File .\scripts\fetch_ffmpeg.ps1
$env:PATH = "$PWD\artifacts\vendor\ffmpeg-pinned\ffmpeg-n8.1.3-23-g5c4f5744cd-win64-lgpl-shared-8.1\bin;$env:PATH"
.venv\Scripts\python.exe -m pytest -q
```

用独立数据目录试运行，避免影响你的正式数据：

```powershell
.venv\Scripts\python.exe -m comfybridge --data-dir .\data-dev configure
```

### 构建

```powershell
pwsh -File .\scripts\build.ps1
```

构建需要 PowerShell 7（`pwsh`），会生成程序目录并封装离线 Setup，详见 [README](<README.md#开发与构建>) 和 [架构说明](<ARCHITECTURE.md#tests-and-release>)。

### 提交规范

- 提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/)，例如 `fix(api): reject duplicate authorization headers`、`docs: clarify cache policy`。
- Pull Request 必须通过 pytest 和 CI。行为变化请补充或更新测试，并在 [更新日志](<CHANGELOG.md>) 中说明用户可见的变化。
- 保持改动聚焦；较大的功能请先开 Issue 讨论。

### 测试与安全规则

- **测试绝不能提交真实 GPU 任务。** 常规测试只使用模拟 ComfyUI。真实实验／烟测脚本必须显式传入授权参数，并通过环境变量 `COMFYBRIDGE_LIVE_COMFY_URL`（或脚本的显式参数，例如 `--comfy-url`）指定 ComfyUI 地址；不要在代码中写死任何真实地址。
- 已有意图记录的实验不得重复执行。
- **不要提交** `config.json`、API Key、数据目录（`data`、`data-dev` 等）、运行时文件、日志、模型权重或参考音频。提交前请检查 `git status`。
- Issue、日志和截图中请隐藏 API Key、内部地址和个人信息。

### 界面文本

- 所有用户可见的界面文本都必须同时提供简体中文和英文，写在各模块的 i18n 文案目录（由 `src/comfybridge/i18n.py` 校验的 `(zh-CN, en-US)` 文案对）中，不要在代码里硬编码单一语言的字符串。
- 外部数据显示前必须经过现有的安全过滤，不要回显任意配置键或请求内容。

### 许可证

提交贡献即表示你同意以本项目的 [MIT 许可证](<LICENSE>) 发布你的贡献。

## English

Thank you for helping improve ComfyBridge! Please read this guide before opening an issue or pull request. Do not report security issues publicly; see the [security policy](<SECURITY.md>).

### Development setup

You need Windows 10/11 x64, [uv](https://github.com/astral-sh/uv) and Python 3.13 (uv can install it for you).

```powershell
uv sync --frozen --python 3.13
.venv\Scripts\python.exe -m pytest -q
```

Optional: fetch the pinned FFmpeg/ffprobe and add its `bin` directory to `PATH` for the current session so the FFmpeg-dependent tests also run (otherwise they are skipped):

```powershell
pwsh -File .\scripts\fetch_ffmpeg.ps1
$env:PATH = "$PWD\artifacts\vendor\ffmpeg-pinned\ffmpeg-n8.1.3-23-g5c4f5744cd-win64-lgpl-shared-8.1\bin;$env:PATH"
.venv\Scripts\python.exe -m pytest -q
```

Use a separate data directory for trial runs so your real data is untouched:

```powershell
.venv\Scripts\python.exe -m comfybridge --data-dir .\data-dev configure
```

### Build

```powershell
pwsh -File .\scripts\build.ps1
```

Building requires PowerShell 7 (`pwsh`). It produces the program directory and wraps it into the offline Setup; see the [README](<README.en.md#development-and-build>) and the [Architecture](<ARCHITECTURE.md#tests-and-release>).

### Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages, for example `fix(api): reject duplicate authorization headers` or `docs: clarify cache policy`.
- Pull requests must pass pytest and CI. Add or update tests for behavior changes, and describe user-visible changes in the [changelog](<CHANGELOG.md>).
- Keep changes focused; please open an issue to discuss larger features first.

### Testing and safety rules

- **Tests must never submit real GPU work.** Regular tests use a mock ComfyUI only. Live experiment/smoke scripts must require explicit authorization flags and take the ComfyUI address from the `COMFYBRIDGE_LIVE_COMFY_URL` environment variable (or an explicit script argument such as `--comfy-url`); never hard-code a real address.
- Never rerun an experiment that already has an intent record.
- **Never commit** `config.json`, API keys, data directories (`data`, `data-dev`, etc.), runtime files, logs, model weights or reference audio. Check `git status` before committing.
- Redact API keys, internal addresses and personal information from issues, logs and screenshots.

### UI strings

- Every user-visible UI string must be provided in both Simplified Chinese and English in the module's i18n catalog (`(zh-CN, en-US)` pairs validated by `src/comfybridge/i18n.py`); do not hard-code single-language strings in code.
- External data must pass through the existing sanitization before display; never echo arbitrary configuration keys or request content.

### License

By contributing, you agree that your contributions are released under the project's [MIT License](<LICENSE>).
