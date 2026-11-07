# ComfyBridge

[简体中文](<README.md>) | English

A local OpenAI-compatible TTS bridge for Windows x86_64 that maps `(model, voice)` to ComfyUI workflows. It supports WAV/MP3, an independent background service, a console Monitor and a configuration wizard.

**Current release: 0.5.1 offline installer. You download a single Setup EXE; installation and updates both happen in place in the same folder.** See [Install and update](<docs/SETUP_040.md>) (Chinese). Timed cache release keeps the [existing rules](<docs/CACHE_030.md>) (Chinese). Model weights, reference audio and production API keys are not included, and the ComfyUI installation and canvas are never modified.

## Features

- OpenAI-compatible API: `POST /v1/audio/speech` and `GET /v1/models` with Bearer API key authentication; works directly with clients such as AstrBot and the OpenAI Python SDK.
- Exact routing of `(model, voice)` to ComfyUI API-format workflows; the text input field, audio output node and cache-control node are bound through menus at import time.
- WAV or MP3 output, transcoded locally by the bundled, pinned FFmpeg/ffprobe.
- Per-model cache policies: follow the workflow, keep cached, release after generation, and timed release (ComfyUI `/free` or a short unload generation).
- Durable task journal: intent is recorded before submission; when the outcome of a submission is unknown, new submissions pause for reconciliation and requests are never replayed automatically.
- Bounded queue, processing slots, body and text length limits, timeouts; logs contain sanitized metadata only.
- Independent background service, live Monitor, bilingual (Simplified Chinese / English) wizard and a vertical numbered home menu; no commands needed for daily use.
- Single-file offline Setup: in-place install, update and same-version repair, with an automatic backup before updating and program-file rollback on failure.

## Requirements

- **Operating system**: Windows 10/11 x64.
- **ComfyUI**: the verified native target is v0.41.3 (see [Architecture](<ARCHITECTURE.md>)). It may run on the same machine or on another machine in your LAN.
- **IndexTTS 2.5 workflows**: install the node pack [ComfyUI-Index-TTS](https://github.com/C10H14N2O5/ComfyUI-Index-TTS) in ComfyUI. It provides `IndexTTS25BaseNode`, `IndexTTS25CacheControlNode` and the emotion nodes needed for cache control (`IndexTTS25EmotionTextNode` / `IndexTTS25EmotionAudioNode` / `IndexTTS25EmotionVectorNode`). Obtain model weights as described by the node pack.
- **Using Setup**: no Python, no PowerShell 7 and no internet connection required.
- **Running from source / building**: Python 3.13 and [uv](https://github.com/astral-sh/uv); building also needs PowerShell 7 (`pwsh`).

## Compatibility

- Tested clients: AstrBot (OpenAI TTS) and the OpenAI Python SDK.
- Primarily tested with IndexTTS 2.5 nodes. Other ComfyUI API-format workflows with a text input and an AUDIO output should work but are unverified.

## Known limitations

- Windows x64 only. POSIX code paths exist in the source, but they are untested and not packaged.
- Python is pinned to 3.13 when running from source.
- `response_format` supports only `wav` and `mp3`.
- `speed` must be omitted or `1`, and `instructions` must be omitted or empty.
- Unknown request fields are rejected with 400, so clients that send extra fields (for example some SillyTavern, Open WebUI or Home Assistant integrations) may be rejected.
- No streaming output and no `/v1/models/{id}`.
- Importing raw WebUI-format workflows supports only the example four-node graph; export other workflows in API format.
- Cache control is supported only for IndexTTS 2.5 nodes.
- Plain HTTP with a single API key, intended for a trusted LAN only; there is no TLS. Do not expose it to the internet.
- The single-instance mutex is scoped to one Windows logon session.
- The EXE is unsigned, so SmartScreen may warn on first run. Verify the download source and SHA-256; do not turn off system security protection.

## Responsible use

- Only clone voices that you have the rights to use or for which you have the speaker's consent. Do not use this project for impersonation, fraud or infringing on others' rights.
- Follow the laws in your jurisdiction on deep synthesis and on labelling AI-generated content; label generated speech clearly where required.

## Quick start

There are two stages: install the program, then perform the first-time initialization.

Initialization guides you through choosing the interface language, entering connection settings (business API key, listen address, port and ComfyUI address), and adding the first model and the first voice workflow.

After saving, the program starts the background service automatically and opens the home menu. Anything you skip can be configured later from the home menu.

### Install

1. Create an empty folder (for example `D:\ComfyBridge`), put the downloaded `Setup.exe` in it and double-click it. First choose the interface language (`[1] 简体中文` or `[2] English`; it applies to this Setup session only), then choose `[1] Install`. The folder must be on a local disk writable by the current user; do **not** use system directories, network drives or sync folders. The installer ships its runtime dependencies, so Python and an internet connection are not required.

2. Check the installation location when prompted and confirm. The location is always the folder that contains Setup, never the current working directory or a temporary extraction directory. A fresh install requires the folder to be empty except for Setup itself.

3. Wait for installation and the local self-test to finish. You will then be asked whether to delete this run's backup. Setup only installs files and does not start the background service, so you may delete Setup afterwards, but you must keep the program and data directories.

### Initialization

Double-click `ComfyBridge.exe` for the first time and complete the wizard below. All settings go into a draft first; at the end they are validated together, a sanitized summary is shown and you are asked to save.

#### Choose the interface language

On first start you are asked to choose `[1] 简体中文` or `[2] English`. The choice is stored per data directory and not asked again; you can change it at any time with `[A] 语言 / Language` in the home menu. The language affects only the display, not the business configuration.

#### Connection settings

1. **Business API key**: the key that clients (for example AstrBot) use to call this bridge. Nothing is echoed while typing. It is required the first time; later, leaving it empty keeps the existing key.

2. **Listen address**: keep the default `127.0.0.1` for same-machine access. To allow other devices, enter this machine's LAN IP, or `0.0.0.0` to listen on all IPv4 addresses. `0.0.0.0` is only a listen address, not a target address for clients.

3. **Listen port**: default `18388`; choose another free port if it is in use.

4. **ComfyUI address**: the address of the machine running ComfyUI, including the scheme (`http://` or `https://`) and the actual port. The wizard pre-fills the local default `http://127.0.0.1:8188`; if ComfyUI runs on another machine, change it to that machine's IP and port.

The bridge never modifies Windows Firewall; allow the listen port yourself if other devices need access.

#### Add the first model

1. When the wizard asks whether to add the first model now (it can also be added later from the home menu), choose `1`. You may skip it, but the speech service will not be ready until a valid voice exists.

2. Enter the model ID. It is also the `model` value of the OpenAI-compatible API, and clients must use the same ID. Model IDs are case-sensitive, may contain only letters, digits, `.`, `_` and `-`, must start with a letter or digit and are at most 64 characters, for example `index-tts-2.5`.

3. Choose a cache policy (it can be changed later with **6 → Edit model or cache policy** in the home menu and takes effect after restarting the background service):

   - **Follow workflow (`inherit`)**: use the cache setting saved in the workflow template; the bridge does not override it, and unloading is decided by the template.

   - **Keep cached (`keep`)**: do not unload the model after each generation; for the example workflow this sets `keep_models_cached` to `true`.

   - **Release after generation (`release`)**: do not keep the cache after each generation; for the example workflow this sets `keep_models_cached` to `false`.

   - **Timed release (`idle_release`)**: normal requests keep the cache according to the cache binding (or the template setting when unbound), and the cache is released after all business tasks have finished and the bridge has been idle for X seconds. X defaults to 300 and may be 1–86400 seconds; a new valid request restarts the countdown. Before saving you must explicitly choose a release method and confirm the risk notice; saving the configuration alone never unloads a model.

     - **ComfyUI /free**: asks ComfyUI to unload models. It affects the whole ComfyUI instance, not a specific model, and may affect other workflows and users of the same instance; a 200 response only means the request was accepted, not that VRAM was freed.

     - **Workflow (short unload generation)**: takes the model's most recently submitted voice preset, sets `keep_models_cached` to `false` and generates a short text once to trigger unloading. This suits models whose cache is managed by a workflow plugin, where `/free` cannot unload reliably. The short text defaults to “好。” and may be changed to 1–64 characters that are not all whitespace. This method consumes real GPU time and may leave audio files on the ComfyUI side. It also requires exactly one IndexTTS 2.5 synthesis node wired to that cache control in the workflow (`IndexTTS25BaseNode` or one of the emotion nodes `IndexTTS25EmotionTextNode` / `IndexTTS25EmotionAudioNode` / `IndexTTS25EmotionVectorNode`), with the text binding on its `text` input or on the `value` of a String / String (Multiline) node linked directly into that input; all voices of the same model must share the releasable cache. When the emotion description of the emotion-text node is empty, the text is analyzed automatically, which may load an additional emotion model.

#### Add the first voice workflow

1. When the wizard asks whether to import the first voice preset now (it can also be added later from the home menu), choose `1`.

2. Select the model you added in the previous step.

3. Enter the voice ID. It is also the `voice` value of the API, and clients must use the same ID. The rules match model IDs and it is case-sensitive, for example `example`.

4. Import the workflow JSON: drag the file into the program window or type/paste its full path (quotes are fine). The program only copies the JSON into the `workflows` subdirectory of the data directory; it does not upload reference audio, copy model files or run a generation, and later changes to the source file are not synchronized. The **model files** and **reference audio** referenced by the workflow must already exist on the ComfyUI machine and be accessible to the corresponding nodes. You can also import the [API example](<examples/workflows/index-tts-2.5_example.json>); it references the reference audio file `example.wav`, which you should replace with audio you have the right to use and place in ComfyUI's `input` directory (or change the file name in the workflow). Exporting **API format** JSON from ComfyUI is generally recommended. Raw WebUI workflows are supported only for the known flat example four-node structure (LoadAudio → IndexTTS25BaseNode + IndexTTS25CacheControlNode → SaveAudio), without subgraphs, bypass or virtual nodes.

5. Complete the following bindings, review the preset summary and confirm:

   - **Text input field**: the node and field that receives the request text. In the example workflow this is `text` on node `2`. If the synthesis node's `text` is supplied by an upstream string node (such as String (Multiline)), choose that string node's `value`; the cache-control node must still be connected to the `cache_control` input of the synthesis node that receives this text.

   - **Audio output node**: the node that outputs the final audio. In the example workflow this is node `4`.

   - **Cache-control node**: the node that holds `keep_models_cached`. In the example workflow this is node `3`. With the follow-workflow policy or "timed release → ComfyUI /free" you may choose not to bind cache control; all other policies require the binding.

6. Review the sanitized configuration summary and confirm saving. If validation fails or you cancel, you stay in the configuration editor with the draft intact, so you can keep editing or discard it.

After a successful save the wizard ends, the program starts or attaches to the background service and opens the home menu. Choose `[1]` (status and live log) and wait until the service is ready before configuring your client. If the background service fails to start, the menu stays available so you can fix the configuration and start it manually; it does not retry automatically over and over. Initialization is then complete, and all daily operations are reachable from the home menu without typing commands.

### Client configuration (example: `OpenAI TTS` in `AstrBot`)

| Setting | Value |
|---|---|
| ID | Any name you like |
| Enable | Turn on after configuration is complete |
| API Key | The same business key you set in the wizard |
| API Base URL | `http://<bridge IP>:<bridge port>/v1` |
| Model ID | `index-tts-2.5` |
| voice | `example` |
| Timeout | Measure it and set it longer than model loading + inference time |

**Do not point the API Base at ComfyUI's port 8188!**

## Home menu: type a number and press Enter

The menu below is shown in Chinese; with English selected the same options appear in English.

```text
ComfyBridge 0.5.1 — 功能选择
------------------------------------------------
状态快照（空回车刷新，选项 1 查看实时状态）

  后台          已验证连接
  ComfyUI 连接  在线
  语音服务就绪  是
  配置          已生效
  语言          简体中文

[1] 查看状态与实时日志
[2] 启动后台
[3] 停止后台
[4] 重启并应用配置

[5] 修改服务连接与 API Key
[6] 管理模型与缓存
[7] 管理音色与工作流
[8] 高级设置
[9] 登录自启动设置

[A] 语言 / Language
[0] 退出窗口（不停止后台）
------------------------------------------------
```

Options: `[1]` status and live log, `[2]` start background, `[3]` stop background, `[4]` restart and apply configuration, `[5]` service connection and API key, `[6]` models and cache, `[7]` voices and workflows, `[8]` advanced settings, `[9]` login startup, `[A]` language, `[0]` close the window (the background keeps running).

- Options 6/7 provide view, add, edit and delete; all menus are vertical.
- Option 8 contains limits/logging settings, the sanitized configuration summary, force stop, uncertain-task reconciliation, cleanup of unused workflow files and cleanup of runtime temporary files.
- In editors, `S` validates and saves, `0`/`Q` returns. With unsaved changes you can save, discard or keep editing.
- Saving the business configuration does not hot-reload it: you can explicitly choose "restart and apply now" or restart later from the home menu. Confirmation prompts default to cancel on an empty Enter.
- On the home screen an empty Enter refreshes the status; the Monitor in option 1 refreshes live and `0`/`Q` returns home. The Monitor shows status and tasks in sections and, by default, the last 10 safe-metadata log entries, with each entry's event and fields on separate lines.
- All four console surfaces share a uniform title, blank lines and indentation; on a real terminal long lines are wrapped to the window width (at most 100 columns), but output redirected to a file or pipe is not wrapped.
- `0`/`Q` in the home menu, exiting the terminal or closing the window never stops the background service. A crashed background service is not revived automatically, and after a manual stop, refreshing the menu does not start it again.
- The language is stored per data directory; switching takes effect immediately in the current window without restarting the service or changing the business configuration.
- No startup shortcut is installed without confirmation. When enabled via option 9, logging in starts the background service and opens the home menu; re-register after moving the directory.

## In-place update and same-version repair

1. Pause AstrBot requests, choose stop background in the old version's menu, wait for tasks to finish, then exit all ComfyBridge windows. **Closing a window alone does not stop the background service.**
2. Put the new Setup EXE into the existing program folder, double-click it, choose the interface language and then `[2] Update`. If files are still in use, follow the prompts and re-check inside Setup; Setup never kills processes.
3. Confirm the version and the data backup location. The default is `data` next to the program; if you previously used `--data-dir`, choose that actual path. This choice is used only for the backup and does not change how the program is launched.
4. Setup first backs up the old program and data, then verifies and replaces the program files. Configuration, workflows, keys and the task journal stay in place; the same version can repair the program, and an older Setup cannot downgrade.
5. When finished you are asked whether to delete this run's backup; the default is to keep it. Keep it at first and start the new version manually to verify speech; a passing local self-test does not mean the real speech service has been verified.
6. If the program and data paths are unchanged, an existing startup shortcut usually needs no change. Setup never creates or rewrites shortcuts; after moving the directory, re-register from the home menu.

If an update is interrupted, run Setup again; it first recovers the unfinished update. Do not delete transaction state or blindly overwrite the task journal; see the [backup and recovery notes](<docs/SETUP_040.md>).

Old and new versions never impersonate each other as the managing instance. If you see an identity-cannot-be-confirmed notice, handle the old background service from the old version's window first; do not guess and kill processes by PID.

### Optional command line (for automation; not needed day to day)

```powershell
.\ComfyBridge.exe
.\ComfyBridge.exe configure
.\ComfyBridge.exe start
.\ComfyBridge.exe monitor
.\ComfyBridge.exe status
.\ComfyBridge.exe restart
.\ComfyBridge.exe stop
.\ComfyBridge.exe --data-dir 'D:\MyBridgeData' monitor
```

- No subcommand: choose the language/configure on first run, start or attach to the background service, then open the home menu.
- `start`: start the background service and exit without occupying the terminal.
- `monitor`: open only the management window; Q, Ctrl+C or closing the window does not stop the background service.
- `restart`: launch an independent controller that stops new requests, drains existing tasks and restarts; closing the Monitor does not abort the controller.
- `stop`: requires explicit confirmation. `stop --force` only force-stops the bridge; remote tasks may continue and the journal is not deleted.
- Saved configuration is not hot-reloaded; the Monitor distinguishes the running configuration from the pending one.
- A crashed background service is not restarted automatically; the Monitor reports the fault and you can start it manually.

Login startup is off by default; the current user's Startup shortcut is changed only when you explicitly run:

```powershell
.\ComfyBridge.exe startup enable
.\ComfyBridge.exe startup disable
```

You can also manage it from menu 9. When enabled, logging in starts the background service and opens the home menu. Re-register after moving the directory; continuing to run while logged out or after signing out is not guaranteed in this version. Scripts and internal background commands never ask for a language and use English when no preference is set; `status` stays machine-readable JSON. Redirected input and output use UTF-8, and piped menus read one option per line.

## Request format

```json
{
  "model": "index-tts-2.5",
  "voice": "example",
  "input": "Hello, this is a speech test.",
  "response_format": "wav"
}
```

- `POST /v1/audio/speech`: returns binary audio as WAV or MP3; MP3 is the default when the format is omitted.
- `GET /v1/models`: returns the deduplicated model list and works even while ComfyUI is offline.
- Both require `Authorization: Bearer <Key>`. The `Bearer` scheme is case-insensitive (`bearer` and `BEARER` work too), while the key itself is compared exactly and is case-sensitive. The API key provides no transport encryption; use HTTP only on a trusted LAN.
- `model`/`voice` are exact and case-sensitive; voices are never dispatched automatically through the model list.
- The default text limit is 4096 Unicode code points and is configurable; text is never split, truncated or rewritten automatically.
- `speed` accepts only omission or the number `1.0`, meaning no extra speed change on top of the workflow preset.
- `instructions` accepts only omission or a blank string; unknown fields, non-neutral parameters, duplicate JSON keys or explicit `null` return 400.

Errors use the OpenAI style: 400 invalid parameters, 401 authentication, 413 request body, 429 queue full, 502 backend/audio failure, 503 not ready/draining/uncertain state, 504 timeout. Automatic SDK retries may generate audio twice; disable automatic retries in clients and do not treat identical text as the same intent.

## Cache policies

See `Quick start - Initialization - Add the first model - 3. Choose a cache policy`.

## Templates and data

Data lives in `data` next to the program by default; use `--data-dir` for another location. An unwritable directory is an error; the program never silently switches directories. The configuration is UTF-8 JSON; see the [example configuration](<examples/config.example.json>). Its key is intentionally empty, so it **cannot be started as a valid production configuration as is**.

- Imported templates are copied into the managed directory and later source changes are not synchronized; replacing one requires reconfirming the bindings and takes effect after a restart.
- API JSON is the standard input; conversion is offered only for the known flat example four-node WebUI graph, without arbitrary subgraphs, bypass or virtual nodes.
- The front-end `control_after_generate=randomize` is not an API input. Business generations keep the template's fixed seed (a notice is shown on import); the timed short unload generation uses its own distinct seed so that the internal unload is not skipped by the execution cache.
- Adding, editing and deleting presets in the wizard never runs a generation; models and reference audio remain managed by ComfyUI.
- Each preset's template is stored as `workflows/preset-<preset ID>-<SHA-256>.json`. Do not edit it by hand, or loading is refused because of a hash mismatch. After deleting a preset, deleting a model (together with its presets) or replacing a template, old templates that are no longer referenced are deleted automatically when **Validate and save** succeeds; exiting without saving deletes only files newly imported in this session that the saved configuration does not use.
- **Menu 8 → Clean up unused workflow files**: lists preset templates in `workflows` that the currently saved configuration does not reference and permanently deletes them after confirmation. Only `preset-*.json` files are handled; other files you placed there are untouched. Templates referenced only by `config.json.bak` are not protected. A running background service is unaffected; when falling back to an old snapshot, the snapshot's template copies are written back.
- **Menu 8 → Clean up runtime temporary files**: lists old startup snapshots in `runtime/snapshots` (each start or restart creates a copy of the configuration and templates) and leftover `.private-*` temporary files older than 10 minutes, and permanently deletes them after confirmation. Snapshots used by the running instance, the last successful start and restart records are kept; the task journal, state, locks and log files are unaffected. The operation is refused while a background start, restart or configuration edit is in progress.
- Backups of the data directory contain the key you configured; treat them as sensitive. Runtime state and snapshots are accessible only to the current user and SYSTEM.
- The single-instance mutex is currently scoped to one Windows logon session; do not use the same data directory from several logon sessions at once.

## Task recovery and safety boundaries

Defaults: 1 processing slot, 8 waiting slots, 300-second total deadline, all configurable. While ComfyUI is temporarily offline the background service stays not ready and rejects new TTS requests; it accepts requests again once the connection is restored and validated.

Each request is journaled with a unique UUID before submission; **a UUID does not make ComfyUI deduplicate automatically**. When the outcome of a submission is unknown, new submissions pause for reconciliation; this cannot be bypassed by restarting the bridge, and old requests are never replayed.

Cancellation only tries to delete this project's own tasks that are still queued; tasks already running finish naturally, and global interrupt or queue clearing is never used. Missing history does not prove that a task did not run. If automatic reconciliation is truly impossible, after confirming the remote state yourself you can choose the exact task in **Menu 8 → Reconcile uncertain tasks** and confirm the risk. Automation can also use the local management command:

```powershell
.\ComfyBridge.exe resolve <task ID>
```

This command is a human acknowledgment that gives up reconciliation; it is not proof that the remote task ended, and it never re-runs anything. Do not confirm casually when unsure.

The bridge keeps no audio history; temporary files are cleaned up after the response finishes or fails, and files on the ComfyUI side are never deleted. Logs record only metadata and sanitized errors, by default 10 MiB per file with 5 backups. **ComfyUI's own history and audio metadata may still contain the original text.**

The management interface binds only to the local loopback address and uses a separate runtime credential. The business key cannot stop, restart or reconfigure the service; there is no remote management interface.

## Development and build

```powershell
uv sync --frozen --python 3.13
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m comfybridge --data-dir .\data-dev configure
pwsh -File .\scripts\build.ps1
```

Building from source requires PowerShell 7 (`pwsh`) and uv; Setup and the installed program need neither Python nor PowerShell 7. `-LegacyRelease <old release directory>` adds a real legacy-package upgrade acceptance run to the build.

Normal tests use a mock ComfyUI and never submit GPU work. Live experiment scripts require explicit user authorization, and experiments that already have an intent record refuse to run again. Test and release requirements are in the [Architecture](<ARCHITECTURE.md#tests-and-release>). Historical experiment and delivery records are available in the Git history.

The build fetches a checksum-pinned FFmpeg/ffprobe, keeps the shared DLLs, licenses and source provenance, first builds and tests the complete program directory, then wraps it into the offline single-file Setup and publishes the EXE only after the installer smoke test passes. The program is unsigned; SHA-256 only checks integrity and does not replace a trusted download source or code signing. Internet-facing deployment, Windows services, concurrent multi-user updates and a general workflow editor are out of scope.

See the [Architecture](<ARCHITECTURE.md>) for the detailed implementation contract, the [Changelog](<CHANGELOG.md>) (Chinese) for version history and the [third-party notes](<docs/THIRD_PARTY.md>) (Chinese) for distribution details.

## Roadmap

The following directions are not scheduled and are not commitments:

- Linux support and a Docker image.
- Python 3.14 support.
- An optional lenient request mode (ignoring unknown fields) for broader client compatibility.
- More output formats: opus, aac, flac, pcm.
- Mapping `speed` to the workflow's `duration_factor`.
- Streaming output.
- `GET /v1/models/{id}`.
- Code signing for the EXE.

## Contributing

Issues and pull requests are welcome. See the [contributing guide](<CONTRIBUTING.md>) for the development setup, commit conventions and test requirements. Do not report security issues in public issues; follow the [security policy](<SECURITY.md>) to report them privately.

## License

This project is released under the MIT License; see [LICENSE](<LICENSE>). Third-party components distributed with the release (FFmpeg, the Python runtime and dependencies, etc.) are governed by their own licenses; see the [third-party notes](<docs/THIRD_PARTY.md>).
