# Example 实验记录（2026-10-23）

## 范围与安全边界

按用户批准计划执行了 **4 次**独立的短中文生成，没有替换现有画布、修改 ComfyUI 安装、清空队列、中断任务或删除服务器输出。原始附件只读；转换后的模板见 [Example API 模板](<../examples/workflows/index-tts-2.5_example.json>)。

独立托管校验图 `comfybridge_example_experiment` 的实时校验通过（4 个节点，0 error，0 warning）。实际实验通过一次性 HTTP POST 提交，携带预生成 UUID 和关联标记；每个案例在提交前落盘意图记录，脚本拒绝覆盖已存在案例，避免盲目重试。

## 已验证绑定

- 节点 1：服务器原有参考音频，未上传或替换参考资源。
- 节点 2 的 `text`：待合成文本。
- 节点 3 的 `keep_models_cached`：已连接节点 2 的 `cache_control`。
- 节点 4 的 `SaveAudio`：实际返回 `outputs["4"].audio`，包含 filename、subfolder、type。
- 源 WebUI 图中的 `upload`、`control_after_generate=randomize` 已过滤。测试保留种子 484884501，不模拟前端随机化。

## 结果

| 案例 | 缓存参数 | 从提交至终态的时间 | 源音频时长 | 状态 |
|---|---:|---:|---:|---|
| baseline | false | 39.871 秒 | 4.226032 秒 | 成功 |
| cached-first | true | 41.091 秒 | 5.746939 秒 | 成功 |
| cached-second | true | 14.269 秒 | 5.143220 秒 | 成功 |
| release | false | 14.265 秒 | 4.585941 秒 | 成功 |

每次均使用不同测试文本，避免相同图输入触发节点结果复用；这些不是等长文本严格基准，不能直接推算固定加速倍数或保证所有请求延迟。最终一次使用 false；随后健康快照报告 torch_vram_total=167772160 bytes，与此前只读空闲快照一致，但未连续采样中间显存，不能据此断言完整缓存实现。

四次均验证：响应 prompt_id 与预生成 UUID 一致，history 中的关联 request_id 一致。该结果证明本机这条正常链路支持相关字段，**不证明重复 ID 会去重**，也不覆盖超时、断网或自定义插件改写时的所有行为。

| 案例 | prompt_id |
|---|---|
| baseline | `2099d7cc-3369-4f62-b407-ff97ed12c58e` |
| cached-first | `2e33fd37-0bdf-4329-8e53-9a22324ec03f` |
| cached-second | `fb082d30-1c17-4c77-9bfe-5f0f8c585183` |
| release | `610f394d-282f-43cd-809f-4c90652b7cb6` |

## 音频格式

源结果均为 **FLAC、22050 Hz、单声道**。baseline 经本机 FFmpeg 转换并由 ffprobe 验证：

- WAV：PCM16、24000 Hz、单声道，4.226042 秒。
- MP3：MP3、24000 Hz、单声道，4.248083 秒（编码填充带来轻微时长差异）。

本地试听产物：[WAV](<../artifacts/experiments/baseline.wav>)、[MP3](<../artifacts/experiments/baseline.mp3>)。这些是明确保留的实验产物，不是运行服务的历史音频库，不纳入默认源码／发行包。

## 尚未验收

- 未由用户实际试听确认主观音色和发音质量。
- 仅 Example 是真实音色；另一个模型／音色路由只能先以模拟测试验证，不能宣称已完成真实双音色切换。
- 尚未操作用户 AstrBot 配置，最终应用端联调需要用户介入。
- 异常窗口（提交响应丢失、取消竞态、进程崩溃等）优先通过可重复的模拟测试，不在用户的 ComfyUI 上故意制造破坏性故障。
