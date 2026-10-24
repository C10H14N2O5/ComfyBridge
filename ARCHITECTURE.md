# Implementation contract — ComfyBridge 0.3.0

This document records the original 2026-10-23 contracts and approved 2026-10-24 extensions. It is not a completion report. The [requirements record](<todo.md>) retains historical decisions. No code may modify ComfyUI installation/canvas, clear global queues/history, or interrupt running GPU jobs. The explicit v0.3.0 `/free` policy is an authorized instance-wide model/memory release, not a targeted per-model operation.

## Scope and milestones

1. Validate supplied Example graph, at most four direct experimental generations. User approved use of the supplied audio workflow. Source attachment remains read-only. Experiment progress is persisted under artifacts/experiments; never rerun a case with an existing intent record.
2. Implement core service and mock failure tests.
3. Implement independent local console monitor, wizard and lifecycle.
4. Build/test a Windows x64 directory distribution; report unverified AstrBot, second real voice, and clean-VM acceptance explicitly.

## Technology and ownership

Python 3.13 isolated uv environment, FastAPI/Pydantic v2/Uvicorn single worker/httpx, SQLite, Rich, pytest/pytest-asyncio, FFmpeg/ffprobe, PyInstaller onedir. No global Python/ComfyUI changes.

Package `src/comfybridge`. Lead owns scaffolding, audio.py, logging_utils.py, build/experiments/docs/integration. Foundation teammate owns config.py, workflows.py and tests/test_config.py,test_workflows.py. Backend teammate owns service.py, api.py, comfy_client.py, journal.py and corresponding tests. Desktop teammate owns launcher.py, control.py, monitor.py, wizard.py, platform_windows.py and corresponding tests. Do not write outside your assigned scopes without messaging lead.

## Shared data/config contract

Pydantic v2 models in config.py (extra=forbid):

- Binding(node_id:str, field:str)
- ServerConfig(listen_host='127.0.0.1', listen_port=18388, api_key:str required/nonempty; no production default key)
- ComfyConfig(base_url='http://127.0.0.1:8188'; HTTP(S), no credentials/query/fragment)
- LimitsConfig(max_input_chars=4096, max_body_bytes=65536, max_inflight=1, max_queue=8, request_timeout=300.0, connect_timeout=3.0, query_timeout=3.0, submit_timeout=15.0, download_timeout=60.0, transcode_timeout=60.0, poll_interval=1.0, probe_interval=5.0, max_audio_bytes=134217728, max_audio_seconds=600.0)
- LoggingConfig(max_bytes=10485760, backup_count=5)
- IdleReleaseConfig(seconds:int=300, strict range1..86400, method='free'|'workflow' required, text:str='好。' nonblank1..64)
- ModelConfig(id:str, cache_policy='inherit'|'keep'|'release'|'idle_release', idle_release:IdleReleaseConfig|None=None); settings required iff idle policy
- PresetConfig(id:canonical UUID str, model:str, voice:str, template:str relative under workflows/, sha256:str, text_binding:Binding, audio_output_node:str, cache_binding:Binding|None=None)
- AppConfig(schema_version=1, server:ServerConfig, comfyui:ComfyConfig, limits:LimitsConfig default, logging:LoggingConfig default, models:list[ModelConfig]=[], presets:list[PresetConfig]=[])

Identifiers model/voice: exact case, ASCII `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. Duplicate models and (model,voice) rejected. IDs are not filenames. No real API key in examples.

Exports:
- `load_config(data_dir:Path) -> AppConfig` reads data_dir/config.json strict JSON (duplicate keys rejected).
- `save_config(data_dir:Path, config:AppConfig) -> None` validate, atomic save + previous version backup; no hot reload. Read existing before overwrite per tooling rules while coding.
- `parse_json_strict(data:str|bytes) -> object` for duplicate keys/nonfinite rejection (can share API).

workflows.py:
- `LoadedPreset` holds `preset:PresetConfig`, `graph:dict`, `cache_policy:str` and `build_prompt(text:str)->dict` deep-copy/pruned effective graph.
- `load_presets(config:AppConfig, data_dir:Path) -> dict[tuple[str,str],LoadedPreset]` validates digest, contained paths, bindings, graph acyclicity and selected output ancestors, no mutation of loaded template.
- `validate_live(loaded:LoadedPreset, schemas:dict[str,dict]) -> list[str]` pure validation; schemas map class type -> object_info schema. Validate required inputs, links/output indices, node existence, expected AUDIO output and relevant combo values. Return sanitized diagnostic codes, never text.
- `convert_ui_workflow(document:dict, schemas:dict|None=None) -> dict` limited flat active LoadAudio/IndexTTS25BaseNode/IndexTTS25CacheControlNode/SaveAudio graph. Named widget values preferred; exact known positional maps only. No bypass, virtual nodes, subgraphs. Discard `upload`, `control_after_generate`; seed stays fixed. Other UI graphs require API export.
- `import_template(data_dir:Path, document:dict, model:str, voice:str, preset_id:str) -> tuple[str,str]` returns relative template path, sha256. Immutable versioned name uses safe prefix + preset id + digest. Copy JSON only, not model/audio assets. Does not register configuration implicitly.

## Backend interfaces

service.py:
- `BridgeService(config:AppConfig, data_dir:Path, *, audio_processor=None)` creates config snapshot, loaded presets, client/journal. Injectable dependencies may be added for tests.
- `async start()`, `async close(force:bool=False)`; don't replay uncertain submissions.
- `status() -> dict` sanitized runtime counts, readiness/reasons, draining, unresolved task summaries (no text/key/whole config).
- `begin_drain() -> None`: no new admissions, existing waiting requests continue under old config.
- `is_drained() -> bool`: true only no live requests and no nonterminal/uncertain remote tasks.
- `async resolve_uncertain(task_id:str) -> None`: explicit local-admin acknowledgment, audit; never POST again.
- `async synthesize(request:SpeechRequest, request_id:str, disconnected:Callable[[],Awaitable[bool]]|None=None) -> AudioResult` creates private temp dir; result owns cleanup method; keep capacity bounded through delivery (API must release result when finished).

api.py:
- `create_app(config:AppConfig, data_dir:Path, service:BridgeService|None=None) -> FastAPI`; put `app.state.bridge=service`; manage service start/close via lifespan when not externally managed.
- `SpeechRequest` strict required model/voice/input str; response_format wav|mp3 default mp3, speed numeric=1 only (not bool), instructions omitted/blank only; null/unknown/duplicate keys rejected. UTF-8 Unicode scalar count <= configured limit, never trim original input except empty check. 64KiB body limit including chunked, API key before parsing.
- GET /v1/models: authenticated OpenAI list, dedup registered models even when Comfy offline.
- POST /v1/audio/speech: raw WAV/MP3, no-store, request-id. Error envelope error.message/type/param/code, sanitized. 400 invalid,401 auth,413 body,429 capacity,502 remote/media,503 unready/draining/uncertain,504 deadline.
- No business management endpoints, no CORS. x-should-retry:false after possible submission; does not guarantee arbitrary client behavior.

## State/protocol requirements

One SQLite journal in data/runtime with transactional submission intent BEFORE network POST, generated canonical UUID prompt_id and extra_data.comfybridge={instance_id,request_id}. Never automatically retry POST /prompt or follow redirects. Same prompt_id does NOT dedupe Comfy. Native supported target v0.41.3.

Persist only IDs, backend URL, template/config version, states/times and sanitized error codes; no input/raw graph/response/audio/API key. Client state separate from backend state. Terminal rows age 7d/max1000; nonterminal never auto-purge. Original backend URL belongs to each task.

Poll queue + history; history status_str success/error is authoritative terminal, completed=false may mean error. GET /history/{id}={} is unknown, NOT canceled. Lost POST response -> reconcile preassigned id, correlation scan if needed (bounded 1000 entries/32MiB); don't resubmit. Unknown pauses new submissions to that target, persists across restart and requires evidence or explicit admin resolution.

Delete only own observed/acknowledged pending via POST /queue {delete:[id]}; 200 not proof. Recheck queue/history, let running finish. Two healthy absent observations AFTER known admission+delete with no outstanding submission can settle NOT_ACTIVE (not prove unexecuted). Never call jobs cancel, /interrupt, global queue clear or history delete. `/free` is only permitted by the explicit idle-release method described below. On crash restart, abandon old HTTP, cancel owned pending safely, follow running, mark terminal without fetching audio, unknown stays blocked. Old backend address change does not clear old tasks.

One processing slot default and 8 waiting. Client deadline300 includes waiting, download/transcode. Active poll1s/offline5s. Local invalid config fails service startup but management UI can repair. Remote schemas per-preset readiness. Offline new TTS503; GET models and monitor work. Existing admitted jobs don't get reposted. No exactly-once promise across client retries.

## Audio contract (lead implementation)

`audio.py`:
- `AudioError(code:str, message:str='Audio processing failed')` sanitized error.
- `AudioResult(path:Path, media_type:str, cleanup_callback:Callable|None=None)`; `async cleanup()` idempotently removes only owned temp directory and invokes callback (await if needed). Service sets callback to release its delivery slot.
- `AudioProcessor(limits:LimitsConfig, ffmpeg:str|Path|None=None, ffprobe:str|Path|None=None)` resolves bundled bin first then environment for source mode.
- `async convert(source:Path, destination:Path, response_format:str, deadline:float) -> AudioResult` monotonic absolute deadline; returns result with cleanup of destination parent (must be per-request dir).

Only selected output node, exactly one audio descriptor. Download /view with validated filename/subfolder/type, no arbitrary URLs,128MiB cap. Local convert ffprobe/FFmpeg args no shell, duration<=600, mono24k WAV PCM16 or MP3 128kbps, strip metadata; reject oversize instead of truncating. Deadline kills only our converter process. Request temp cleaned after send/abandon, never delete Comfy files. Comfy itself can retain text in history/audio metadata.

logging_utils.py: configure_logging(data_dir, LoggingConfig), white-list structured event helper; rotating10MiB+5, message<=4KiB, no raw exceptions/tracebacks upstream messages containing text. Backend may use standard logger but log only hard-coded event/code/IDs.

## Desktop lifecycle contract

CLI via `comfybridge.launcher:main`; __main__ calls same. Data default executable sibling data (source root/data), --data-dir explicit. Plain run resolves the per-data-directory language preference, guides first configuration if needed, attempts start-or-attach exactly once, then opens the numbered home menu. Returning/refreshing never automatically starts again. Commands serve/start/monitor/configure/status/stop/restart, startup enable/disable, explicit unresolved-task acknowledgment.

Backend independent DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP, stdio DEVNULL, file logging. Windows per-data-dir mutex; validate service identity and version not just PID. Local control separate127.0.0.1 ephemeral port (not public port), ephemeral private token in current-user/SYSTEM ACL protected runtime state; business key never authorizes admin. Reject cross-origin and unexpected Host. No secret args/logs. Control routes status, drain/stop, resolve; service exposed via app.state.bridge.

Transient independent restart controller validates proposed config, records last-known-good snapshot incl immutable templates, drains old backend, waits remote terminal, starts new; one fallback to last good only for local startup failure, clear pending config failure in status. Monitor close cannot stop controller. No crash watchdog. Explicit force stop stops only Bridge and preserves journal; unresolved tasks require manual acknowledgment before unblocking. Configuration writer serialized separately from service lock.

Monitor refresh2s/logtail50 configurable in implementation if appropriate, Q/CtrlC/window close monitor only; explicit confirmed stop. Wizard config edit plus model/preset CRUD, file import conversion, menu-select input/output/cache bindings, validate then save; no generation. Current config vs pending shown. Startup current-user shortcut opt-in starts backend+home; no default install, migration needs re-register.

0.2.0 UI: independent strict atomic ui.json stores zh-CN/en-US under an independent ui writer lock; it never participates in business configuration digests or service snapshots. Initial language abort prevents automatic startup. All normal operations are reachable from the vertical home menu, with explicit default-cancel stop/restart/force/acknowledgement confirmations and save/discard/continue on dirty editor return. Embedded Monitor Q/0 returns home; observer exit never stops service. Shared ConsoleInput avoids mixing buffered TextIO with raw reads: real consoles keep nonblocking immediate Monitor keys; redirected UTF-8 input consumes complete command lines before confirmations and uses non-prefetching prompts. Background/internal commands never ask language; status JSON/canonical IDs/codes remain unchanged. Known-field display and diagnostic-location allowlists avoid echoing arbitrary configuration keys or payloads. See [UI contract](<docs/UI_020_PLAN.md>) and [verification](<docs/UI_020_VALIDATION.md>).

## 0.3.0 idle-release contract

The fourth model policy is explicitly configured from the existing cache menu, not inferred from node names or VRAM. All admitted business requests invalidate the shared idle epoch; only complete local business/remote/recovery/delivery quiescence starts the full per-model duration using a monotonic clock. Never-used models are not cleaned. External running/pending queues postpone both methods. A local activity generation is rechecked after asynchronous preflight, and a single cleanup gate prevents business POSTs racing any cache operation even when max_inflight > 1. Work already submitted is not canceled merely because new business arrives; normal capacity/deadlines still apply. Cleanup activity does not rearm itself.

- `ComfyClient.free()` performs exactly one POST with unload_models/free_memory true, accepts native200 empty body, follows no redirect and makes no physical-VRAM guarantee. It coalesces dirty free-mode models for the same URL; earliest expiry may release other models early. It cannot prove custom global plugin caches were unloaded and does not clear workflow-mode records. Remote query/POST cannot lock other clients.
- Workflow cleanup uses the latest actually submitted preset of the model. The user-tested same-model/different-reference workflow copies share a releasable cache; different weights/plugins are outside that assumption. Normal business injects keep=true when bound; free mode may omit the binding. `LoadedPreset.build_cleanup_prompt(text, seed)` deep-copies the selected ancestors, injects false and a strictly validated positive uint32 seed, and never changes business templates. The durable nonwrapping sequence belongs to the journal. The supported cleanup synthesis node is IndexTTS25BaseNode with validated text/cache/seed; unsupported nodes fail explicitly.
- Workflow cleanup is an internal purpose-tagged task with transactional intent BEFORE a single POST, original-URL correlation and existing conservative recovery. No HTTP caller is not a disconnect. No audio download/transcode/delivery for cleanup. Remote SaveAudio artifacts are not deleted. On expiry only owned observed-pending deletion is allowed; running/unknown remains tracked and gates dispatch until terminal evidence or explicit admin acknowledgment.
- SQLite additive migration retains old speech task rows and stores safe model/preset/template/URL/cycle identity, no text/graph/audio/key. Cache-cycle completion and task terminal transitions are atomic. `/free` has its own durable operation intent/outcome, not a fictional prompt ID recoverable from history. Unknown/failed attempts are not replayed per cycle; new real business use of that model creates a new opportunity.
- On restart reconcile unfinished tasks, then wait a full interval for matching pending cycles. Do not guess cleanup from changed/deleted templates or a different URL; mark stale until new use. Drain prevents new cleanup; force preserves unfinished records. Closing the observer leaves timers running.
- Status adds safe `cache` rows and task purpose without changing speech/models protocol. Distinguish requested /free versus completed workflow versus measured VRAM; bilingual UI and diagnostics use allowlists. Details: [cache guide](<docs/CACHE_030.md>) and [acceptance](<docs/CACHE_030_VALIDATION.md>).

## Tests and release

No tests submit real GPU work implicitly. Mock all error/race/restart cases. Lead authorizes any further true generation beyond four direct cases; core SDK smoke may use mocks initially. Only one genuine voice supplied, do not mislabel a second alias as different voice. No installed AstrBot known: provide setup guide and mark actual integration pending if user absent. No clean VM available: isolated PATH checks not claimed as clean-system test.

Directory release includes EXE, Python/runtime deps, pinned FFmpeg/ffprobe distribution plus licenses/source provenance/checksums. Local FFmpeg is GPL/shared; do not copy only its exe assuming standalone. Prefer audited pinned LGPL build for release. No real keys or reference audio in release. Include README, examples, experiment/test report, update todo with actual statuses.
