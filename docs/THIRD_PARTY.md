# 第三方组件与本机构建分发说明

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

**这不是完整法律审查，也不声称两个源码归档已经包含全部启用的第三方库源码。** 构建配方还引用多个第三方依赖，各有许可、版权和源码获取要求。当前目录包用于用户本机构建和测试；若要公开再分发，应进一步审核完整依赖源码可获得性、相关 notice 及对应许可证义务，不只附一个 LICENSE 文件就声称全部合规。

## Python 与打包工具

Python 解释器随 PyInstaller 目录包分发，无需目标机安装系统 Python。构建时收集环境中第三方发行包的版本、许可表达式、项目链接及可找到的 license／notice 文件，放入发行包 `licenses/python`。该清单可能包含仅供测试和构建的依赖，是保守的环境清单，不代表每个包都被 EXE 使用。

本项目没有改动 ComfyUI、IndexTTS 模型代码或权重，也不在发行包中分发参考音频。使用模型、节点和音色资源时应遵守它们各自的使用条件。

## 不包含的承诺

- 目录包没有代码签名；Windows 可能提示未知发布者。不要为此关闭系统安全保护。
- 源码与构建脚本保留供审查，自动生成的 checksum 用于检测内容变化，不等同于可信发布者签名。
- 不承诺公网安全部署、多租户隔离或通用不可信工作流沙箱。模板应仅来自本机管理员信任的来源。
