# 第三方组件与本机构建分发说明

ComfyBridge 本身以 MIT 许可证发布，见 [LICENSE](<../LICENSE>)。下列第三方组件随发行包分发或被引用，遵循各自的许可证。

本项目通过独立进程调用 FFmpeg／ffprobe，不将 FFmpeg 作为 Python 进程内库加载。Windows 目录包保留上游共享 DLL，不能只取两个 EXE 后删除 DLL。

## FFmpeg 固定构建

- 上游：[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)。
- 固定发布：`autobuild-2026-10-22-13-06`。
- 构建：`ffmpeg-n8.1.3-23-g5c4f5744cd-win64-lgpl-shared-8.1.zip`。
- SHA-256：`c41df73a3f3259e81d5d53e6ce0c4c80b4841a30894fb77954650e445daa6c53`。
- FFmpeg 源码提交：[5c4f5744cd60943125a78db692b728210102f255](https://github.com/FFmpeg/FFmpeg/tree/5c4f5744cd60943125a78db692b728210102f255)。
- 构建配方提交：[937868360a21d711ec94a33f5591d345ad9b7754](https://github.com/BtbN/FFmpeg-Builds/tree/937868360a21d711ec94a33f5591d345ad9b7754)。

下载与校验规则见 [组件清单](<../scripts/ffmpeg-manifest.json>) 和 [获取脚本](<../scripts/fetch_ffmpeg.ps1>)。已在本机核实该构建未开启 `--enable-gpl`，开启 `--enable-version3`，包含 `libmp3lame` 与 PCM16 编码器，原包许可文本为 LGPL v3。

构建脚本随目录包复制上游 LGPL 文本、GPL v3 文本、FFmpeg 对应源码归档及构建配方归档，附 Python 依赖许可清单与 notice。源码归档的固定哈希在 [源码获取脚本](<../scripts/fetch_sources.ps1>) 校验。

**这不是完整法律审查，也不声称两个源码归档已经包含全部启用的第三方库源码。** 构建配方还引用多个第三方依赖，各有许可、版权和源码获取要求；公开发布时的处理方式见下文 [公开发布清单](<#公开发布清单>)。不能只附一个 LICENSE 文件就声称全部合规。

## Python 与打包工具

Python 解释器随 PyInstaller 目录包分发，无需目标机安装系统 Python。构建时收集环境中第三方发行包的版本、许可表达式、项目链接及可找到的 license／notice 文件，放入发行包 `licenses/python`。该清单可能包含仅供测试和构建的依赖，是保守的环境清单，不代表每个包都被 EXE 使用。

PyInstaller 自身以 GPL 2.0 发布，但其 bootloader 附带例外条款，允许将其与任意许可证的程序一同分发；本项目的 EXE 和 Setup 依赖该例外。

本项目没有改动 ComfyUI、IndexTTS 模型代码或权重，也不在发行包中分发参考音频。使用模型、节点和音色资源时应遵守它们各自的使用条件。

## 公开发布清单

每个公开 Release（GitHub 及自建 Gitea 镜像）按以下方式处理第三方材料：

- **FFmpeg 源码与构建配方**：除 Setup 内已附带的副本外，每个 Release 另外把 FFmpeg 对应源码归档和 BtbN 构建配方归档作为独立的 Release 附件上传。两者来自 `artifacts/vendor/sources`，由 [源码获取脚本](<../scripts/fetch_sources.ps1>) 按固定哈希下载和校验。
- **BtbN 构建中启用的第三方库**：例如 `libmp3lame` 等库的上游源码，通过上文固定的构建配方提交（[937868360a21d711ec94a33f5591d345ad9b7754](https://github.com/BtbN/FFmpeg-Builds/tree/937868360a21d711ec94a33f5591d345ad9b7754)）中记录的上游地址和版本引用；发行包同时附带对应的许可文本。
- **Python 运行时与依赖**：许可清单和可找到的 license／notice 文件随发行包放在 `licenses/python`。
- **PyInstaller bootloader**：GPL 许可并附带允许分发的例外条款，见上文。
- **不分发的内容**：[ComfyUI-Index-TTS](https://github.com/C10H14N2O5/ComfyUI-Index-TTS) 节点包、模型权重和参考音频均不随本项目分发，由用户自行获取，受其各自许可证和使用条件约束。
- **项目自身**：源码以 MIT 许可证发布，见 [LICENSE](<../LICENSE>)。

以上是维护者的发布做法记录，**不构成法律意见**。如需确认具体义务，请咨询专业人士。

## 不包含的承诺

- 目录包没有代码签名；Windows 可能提示未知发布者。不要为此关闭系统安全保护。
- 源码与构建脚本保留供审查，自动生成的 checksum 用于检测内容变化，不等同于可信发布者签名。
- 不承诺公网安全部署、多租户隔离或通用不可信工作流沙箱。模板应仅来自本机管理员信任的来源。
