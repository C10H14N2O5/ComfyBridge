# Approved 0.2.0 UI implementation contract

The approved change is a bilingual (zh-CN/en-US), vertical, numbered console UI. Normal double-click selects language once, runs first setup if needed, attempts one automatic start/attach, then opens a persistent home menu. No real GPU tests, user Startup changes, firewall changes, or changes to business API/schema/task recovery are authorized for this revision.

## Shared interfaces (coordinate changes with Lead)

### i18n.py / ui_preferences.py (foundation owner)
- `UIContext(language='en-US')`; `.set_language(language)` validates zh-CN/en-US.
- `.t(catalog, key, **values) -> str`: each module owns a `MESSAGES` mapping from stable key to `(Simplified Chinese, English)` pair; static format placeholders must match between languages. `validate_catalog(catalog)` tests parity. UI outputs must disable Rich markup and sanitize external data.
- `safe_text(value, max_length=...) -> str`: strips terminal controls/escapes from external labels, no translation of IDs/paths.
- `UIPreferences(schema_version=1, language=...)`; `load_ui_preferences(data_dir)` returns None if absent, raises sanitized `UIPreferenceError` for bad data.
- `save_ui_preferences(data_dir, preferences)`: bounded strict JSON, confined no-symlink destination, atomic save under short independent `ui` lock; reuse existing helpers; no changes to config.json or backend snapshot.
- `resolve_ui(data_dir, *, interactive, ask=input, output=print) -> UIContext`: load valid preference; only interactive missing/invalid preference prompts explicit bilingual language selection. Noninteractive fallback English, no new persisted choice. A write failure leaves current-session selection working with honest warning.
- `select_language(data_dir, ui, *, ask=input, output=print, allow_cancel=True) -> bool`: `[1] 简体中文`, `[2] English`, `[0] cancel` when allowed; persist valid choice and update ui immediately; invalid values retry; EOF/KeyboardInterrupt must not persist an unmade choice.
- `is_interactive() -> bool`: actual console detection, not just Windows NUL isatty; script/background entry points never choose language.

### wizard.py (desktop owner)
- Keep existing public pure CRUD/import/validation functions and their behavior.
- `diagnostic(error, ui=None)` remains sanitized and defaults to English for compatibility.
- `Wizard(data_dir, *, ui=None, ask=None, secret=None, output=None)` extends existing signature.
- `run_wizard(data_dir, *, ui=None, section=None, first_run=False, **io) -> int`: 0 saved, 1 cancelled/not-saved; section None keeps full configure editor, section one of server/models/presets/limits opens a scoped vertical editor.
- First run: server/key then optional first model and preset, summary, validated explicitly confirmed save. Models/presets can be deferred with clear not-ready message. Never guess text/output bindings or run generation.
- Existing config invalid: keep original until explicit save/backup; retain drafts on correction.
- Main/scoped editor: vertical menus, `S` save, `0`/Q return; if dirty, offer save/discard/continue. Preserve legacy numeric mapping for full configure where feasible. Confirmations show 1/0 and accept Y/y/N/n for existing CLI/test callers. Returning an invalid input retries locally.
- ui change is owned by home language menu; wizard uses same UIContext, no global translator/state.
- Saving never restarts; Lead home handles separate optional restart prompt.

### monitor.py (backend owner for this revision)
- `monitor(data_dir, *, ui=None, return_to_menu=False, ...existing args) -> int` defaults English, standalone Q exits, embedded Q/0 returns to home; never stop backend due observer exit.
- Preserve tested Windows console/pipe/EOF/NUL nonblocking read_key. Existing management hotkeys may stay as aliases; rendered controls must be vertical and bilingual, confirmations explicit.
- `format_status(status: dict|None, ui: UIContext) -> list[str]`: localized human-readable known fields, no default raw JSON or key/token.
- Localized logs render known metadata/event codes; unknown codes stay safe canonical codes. Do not modify on-disk log format or trust arbitrary log strings as Rich markup.

### console_ui.py / launcher.py (Lead owner)
- Home options: 1 monitor; 2 start; 3 drain/stop; 4 restart; 5 server/key editor; 6 models editor; 7 presets editor; 8 advanced; 9 startup; A language; 0/Q close observer.
- Advanced: limits/logging editor; sanitized config summary; force stop; uncertain task selection + acknowledgement. No direct PID kills.
- Existing lifecycle APIs (`ensure_backend`, `request_stop`, `request_restart`, ControlClient) reused.
- Status refreshed on page entry/action/empty Enter, monitor is realtime. Auto-start once only, no crash watchdog or restart after manual stop.
- Start errors retain usable menu. Unconfirmed/identity mismatch must not be represented as confidently stopped.
- Existing CLI commands, flags and status JSON stay compatible. Internal serve/controller skip UI; human configure/monitor load UI preference; script start does not prompt for language.

## Owners and safety

Foundation writes i18n.py/ui_preferences.py and their tests only. Desktop writes wizard.py/tests/test_wizard.py only. Backend writes monitor.py/tests/test_monitor.py only. Lead writes console_ui.py/launcher.py, matching integration tests, version/build/docs. Each module owns its translation catalog so there are no shared catalog write conflicts.

Test all flows with injected I/O, mock ComfyUI and isolated local children. Extend packaged smoke for both languages/home/CLI/observer lifecycle, preserve no-Python-PATH audio test. Ship 0.2.0 independently of 0.1.0; existing config survives language migration. Users must stop old backend with old UI before changing executable versions. Keep previous clean-VM/real Startup limitations explicit.
