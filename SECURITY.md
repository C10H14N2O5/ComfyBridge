# 安全策略 / Security policy

[中文](#中文) | [English](#english)

## 中文

### 报告漏洞

请通过 GitHub 私密安全公告报告漏洞：<https://github.com/C10H14N2O5/ComfyBridge/security/advisories/new>。

**不要**在公开 Issue、Pull Request 或讨论区披露漏洞细节。报告中请尽量包含受影响版本、复现步骤和影响说明，并隐藏 API Key、`config.json` 内容、内部地址等敏感信息。维护者会尽快确认收到并与你协商修复与披露时间。

### 支持的版本

只有最新发布的版本会接收安全修复。请先升级到最新版本再确认问题是否仍然存在。

### 适用范围说明

- ComfyBridge 设计用于**可信局域网**：业务接口使用明文 HTTP 和单一 API Key，不提供 TLS。**不要将其暴露到公网**；需要跨网络访问时，请自行放在 VPN 或带 TLS 的反向代理之后。
- 管理接口仅绑定本机回环地址，使用独立的运行期凭证；业务 API Key 无法用于管理操作。
- 工作流模板应只来自你信任的来源；本项目不是通用的不可信工作流沙箱。
- 通过公网暴露服务、局域网内被动嗅探明文流量、ComfyUI 自身或第三方节点包的问题，不属于本项目的漏洞范围；ComfyUI 和节点包的问题请向其上游报告。

## English

### Reporting a vulnerability

Please report vulnerabilities privately through a GitHub security advisory: <https://github.com/C10H14N2O5/ComfyBridge/security/advisories/new>.

Do **not** disclose vulnerability details in public issues, pull requests or discussions. Include the affected version, reproduction steps and impact where possible, and redact API keys, `config.json` contents, internal addresses and other sensitive data. The maintainer will acknowledge the report as soon as possible and coordinate a fix and disclosure timeline with you.

### Supported versions

Only the latest release receives security fixes. Please upgrade to the latest release and check whether the issue still reproduces.

### Scope notes

- ComfyBridge is designed for a **trusted LAN**: the business API uses plain HTTP with a single API key and has no TLS. **Do not expose it to the internet**; if you need access across networks, put it behind a VPN or a TLS reverse proxy yourself.
- The management interface binds only to the local loopback address and uses a separate runtime credential; the business API key cannot perform management operations.
- Workflow templates should come only from sources you trust; this project is not a general sandbox for untrusted workflows.
- Exposing the service to the internet, passive sniffing of plain-HTTP traffic on the LAN, and issues in ComfyUI itself or in third-party node packs are out of scope; report ComfyUI and node-pack issues to their upstream projects.
