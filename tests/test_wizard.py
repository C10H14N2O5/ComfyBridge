import copy
import json
from pathlib import Path
import uuid

import pytest

from comfybridge.config import AppConfig, ComfyConfig, ServerConfig, save_config, load_config
from comfybridge.wizard import MESSAGES, Wizard, binding_choices, delete_model, delete_preset, diagnostic, node_choices, put_model, put_preset, read_import, run_wizard, summary, validate_draft
from comfybridge.i18n import UIContext, validate_catalog


def draft():
    return AppConfig(server=ServerConfig(api_key='never-print-this-key'), comfyui=ComfyConfig()).model_dump(mode='json')


def graph():
    return {
        '1': {'class_type': 'IndexTTS25CacheControlNode', 'inputs': {'keep_models_cached': False}},
        '2': {'class_type': 'IndexTTS25BaseNode', 'inputs': {'text': 'private-template-text', 'cache_control': ['1', 0]}},
        '3': {'class_type': 'SaveAudio', 'inputs': {'audio': ['2', 0], 'filename_prefix': 'audio'}}}


def test_diagnostics_show_location_without_secret_input():
    from pydantic import ValidationError
    data = draft()
    data['server']['api_key'] = 'TOP SECRET KEY'
    with pytest.raises(ValidationError) as caught:
        AppConfig.model_validate(data)
    message = diagnostic(caught.value)
    assert 'server.api_key' in message
    assert 'TOP SECRET' not in message
    assert 'missing' in diagnostic(FileNotFoundError('private file path')).lower()
    assert 'private file path' not in diagnostic(FileNotFoundError('private file path'))


def test_model_crud_duplicate_and_cache_policy():
    data = draft()
    put_model(data, 'm')
    assert data['models'] == [{'id': 'm', 'cache_policy': 'inherit'}]
    with pytest.raises(ValueError): put_model(data, 'm')
    put_model(data, 'renamed', 'keep', replace='m')
    assert data['models'][0]['cache_policy'] == 'keep'
    delete_model(data, 'renamed')
    assert data['models'] == []
    with pytest.raises(ValueError): put_model(data, '../bad')


def test_preset_import_update_remove_keeps_immutable_copy(tmp_path):
    data = draft()
    put_model(data, 'm', 'keep')
    arguments = dict(model='m', voice='v', text_binding={'node_id': '2', 'field': 'text'}, audio_output_node='3', cache_binding={'node_id': '1', 'field': 'keep_models_cached'})
    source = graph()
    p = put_preset(tmp_path, data, source, **arguments)
    immutable = (tmp_path / p['template']).read_bytes()
    source['2']['inputs']['text'] = 'modified source'
    assert (tmp_path / p['template']).read_bytes() == immutable
    with pytest.raises(ValueError): put_preset(tmp_path, data, source, **arguments)
    updated = put_preset(tmp_path, data, source, replace_id=p['id'], **arguments)
    assert updated['id'] == p['id'] and updated['template'] != p['template']
    assert len(data['presets']) == 1
    validate_draft(tmp_path, data)
    with pytest.raises(ValueError): delete_model(data, 'm')
    delete_preset(data, p['id'])
    assert not data['presets'] and (tmp_path / p['template']).exists()


def test_candidate_menus_dont_dump_private_text_or_key():
    assert 'private-template-text' not in repr(binding_choices(graph()))
    assert 'private-template-text' not in repr(node_choices(graph()))
    assert 'never-print-this-key' not in repr(summary(draft()))
    assert binding_choices(graph(), cache=True)[0][0] == {'node_id': '1', 'field': 'keep_models_cached'}


def test_invalid_binding_does_not_register_preset(tmp_path):
    data = draft()
    put_model(data, 'm')
    with pytest.raises(ValueError):
        put_preset(tmp_path, data, graph(), model='m', voice='v', text_binding={'node_id': '3', 'field': 'filename_prefix'}, audio_output_node='3')
    assert data['presets'] == []


def test_wizard_save_no_restart_no_startup_no_generation(tmp_path, monkeypatch):
    initial = AppConfig.model_validate(draft())
    save_config(tmp_path, initial)
    answers = iter(['1', 's', 'Y'])
    output = []
    monkeypatch.setattr('comfybridge.control.get_status', lambda _: None)
    monkeypatch.setattr('comfybridge.platform_windows.configure_startup', lambda *_a, **_k: pytest.fail('startup'))
    wizard = Wizard(tmp_path, ask=lambda _: next(answers), secret=lambda _: pytest.fail('secret unnecessary'), output=output.append)
    assert wizard.run() == 0
    assert load_config(tmp_path) == initial
    assert 'never-print-this-key' not in '\n'.join(output)
    assert 'pending' in output[-1]


def test_wizard_external_change_not_overwritten(tmp_path, monkeypatch):
    save_config(tmp_path, AppConfig.model_validate(draft()))
    answers = iter(['s', 'Y'])
    def ask(prompt):
        answer = next(answers)
        if answer == 'Y':
            changed = draft()
            changed['server']['listen_port'] = 19999
            save_config(tmp_path, AppConfig.model_validate(changed))
        return answer
    assert Wizard(tmp_path, ask=ask, output=lambda _: None).run() == 1
    assert load_config(tmp_path).server.listen_port == 19999


def test_api_import_strict_and_unknown_ui_rejected(tmp_path):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(graph()))
    assert read_import(source) == graph()
    source.write_text('{"a":1,"a":2}')
    with pytest.raises(ValueError): read_import(source)
    source.write_text(json.dumps({'nodes': [{'id': 1, 'type': 'UnknownNode'}], 'links': []}))
    with pytest.raises(ValueError): read_import(source)


def test_known_named_ui_import_without_network(tmp_path):
    document = {'nodes': [
        {'id': 1, 'type': 'LoadAudio', 'widgets_values_named': {'audio': 'reference.wav', 'upload': 'ignored'}},
        {'id': 2, 'type': 'SaveAudio', 'widgets_values_named': {'filename_prefix': 'audio'},
         'inputs': [{'name': 'audio', 'type': 'AUDIO', 'link': 1}]}],
        'links': [[1, 1, 0, 2, 0, 'AUDIO']]}
    source = tmp_path / 'known-ui.json'
    source.write_text(json.dumps(document))
    result = read_import(source)
    assert result['1']['inputs'] == {'audio': 'reference.wav'}
    assert result['2']['inputs']['audio'] == ['1', 0]


def test_full_preset_menu_explicit_binding_selection(tmp_path, monkeypatch):
    data = draft()
    put_model(data, 'm', 'keep')
    save_config(tmp_path, AppConfig.model_validate(data))
    source = tmp_path / 'import.json'
    source.write_text(json.dumps(graph()))
    answers = iter(['6', '1', 'v', str(source), '1', '3', '2', 'Y', 's', 'Y'])
    monkeypatch.setattr('comfybridge.control.get_status', lambda _: None)
    messages = []
    assert Wizard(tmp_path, ask=lambda _: next(answers), output=messages.append).run() == 0
    saved = load_config(tmp_path)
    assert saved.presets[0].text_binding.node_id == '2'
    assert saved.presets[0].audio_output_node == '3'
    assert saved.presets[0].cache_binding.node_id == '1'
    assert saved.models[0].cache_policy == 'keep'
    assert 'private-template-text' not in '\n'.join(messages)


class ScriptedIO:
    def __init__(self, answers, secrets=()):
        self.answers = iter(answers)
        self.secrets = iter(secrets)
        self.prompts = []
        self.messages = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return next(self.answers)

    def secret(self, prompt):
        self.prompts.append(prompt)
        return next(self.secrets)

    def options(self):
        return {'ask': self.ask, 'secret': self.secret, 'output': self.messages.append}


@pytest.fixture
def no_actions(monkeypatch):
    monkeypatch.setattr('comfybridge.control.get_status', lambda _: None)
    def forbidden(*args, **kwargs):
        pytest.fail('Wizard must not start/restart/generate/change Startup')
    monkeypatch.setattr('comfybridge.launcher.ensure_backend', forbidden)
    monkeypatch.setattr('comfybridge.launcher.request_restart', forbidden)
    monkeypatch.setattr('comfybridge.platform_windows.configure_startup', forbidden)


def test_wizard_catalog_pairs_and_placeholders():
    validate_catalog(MESSAGES)
    assert len(MESSAGES) >= 80
    assert set(MESSAGES) >= {'saved_pending', 'dirty', 'first_model', 'first_preset'}


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_first_run_can_defer_models_and_presets(tmp_path, language, no_actions):
    io = ScriptedIO(['', '', '', '0', '1'], ['manual-business-key'])
    ui = UIContext(language)
    assert run_wizard(tmp_path, ui=ui, first_run=True, **io.options()) == 0
    config = load_config(tmp_path)
    assert config.server.api_key == 'manual-business-key'
    assert config.models == [] and config.presets == []
    assert ui.t(MESSAGES, 'deferred') in io.messages
    assert ui.t(MESSAGES, 'saved_pending') == io.messages[-1]
    assert 'manual-business-key' not in '\n'.join(io.messages + io.prompts)
    assert not (tmp_path / 'runtime/control.json').exists()


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_first_run_guides_model_preset_and_explicit_bindings(tmp_path, language, no_actions):
    source = tmp_path / 'first.json'
    source.write_text(json.dumps(graph()))
    io = ScriptedIO(['', '', '', '1', 'new-model', '1', '1', '1', 'new-voice', str(source),
                     '1', '3', '2', '1', '1'], ['manual-key'])
    assert run_wizard(tmp_path, ui=UIContext(language), first_run=True, **io.options()) == 0
    config = load_config(tmp_path)
    assert config.models[0].id == 'new-model'
    assert config.presets[0].voice == 'new-voice'
    assert config.presets[0].text_binding.node_id == '2'
    assert config.presets[0].audio_output_node == '3'
    assert config.presets[0].cache_binding.node_id == '1'
    assert 'private-template-text' not in '\n'.join(io.messages)


def test_first_run_invalid_fields_retry_in_place_without_echoing_key(tmp_path, no_actions):
    io = ScriptedIO(['bad host', '127.0.0.1', 'bad', '0', '18388', 'bad-url', 'http://127.0.0.1:8188',
                     '0', '1'], ['TOP SECRET BAD KEY', 'valid-manual-key'])
    assert run_wizard(tmp_path, ui=UIContext('zh-CN'), first_run=True, **io.options()) == 0
    assert load_config(tmp_path).server.listen_port == 18388
    assert 'TOP SECRET' not in '\n'.join(io.messages)
    assert 'server.api_key' not in '\n'.join(io.messages)  # field validator here is ServerConfig
    assert any('api_key' in line for line in io.messages)
    assert sum('业务 API Key' in prompt for prompt in io.prompts) == 2


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_first_run_refused_save_keeps_draft_until_explicit_discard(tmp_path, language, no_actions):
    io = ScriptedIO(['', '', '', '0', '0', 'q', '2'], ['valid-key'])
    ui = UIContext(language)
    assert run_wizard(tmp_path, ui=ui, first_run=True, **io.options()) == 1
    assert not (tmp_path / 'config.json').exists()
    assert ui.t(MESSAGES, 'dirty') in io.messages


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_scoped_models_vertical_add_edit_delete_and_save(tmp_path, language, no_actions):
    save_config(tmp_path, AppConfig.model_validate(draft()))
    io = ScriptedIO(['2', 'm', '1', '3', '1', 'm-renamed', '2', '1',
                     '4', '1', '0', 's', 'y'])
    ui = UIContext(language)
    assert run_wizard(tmp_path, ui=ui, section='models', **io.options()) == 0
    config = load_config(tmp_path)
    assert config.models[0].id == 'm-renamed'
    assert config.models[0].cache_policy == 'keep'
    assert '[2] ' + ui.t(MESSAGES, 'add_model') in io.messages
    assert not any(' | ' in message for message in io.messages)
    assert not any(ui.t(MESSAGES, 'business_key') == prompt for prompt in io.prompts)


@pytest.mark.parametrize('exit_choice,expected_saved', [('1', True), ('2', False)])
def test_dirty_return_save_or_discard(tmp_path, exit_choice, expected_saved, no_actions):
    save_config(tmp_path, AppConfig.model_validate(draft()))
    answers = ['2', 'm', '1', '0', exit_choice] + (['1'] if expected_saved else [])
    io = ScriptedIO(answers)
    assert run_wizard(tmp_path, section='models', **io.options()) == (0 if expected_saved else 1)
    assert bool(load_config(tmp_path).models) is expected_saved


def test_dirty_continue_keeps_model_then_saves(tmp_path, no_actions):
    save_config(tmp_path, AppConfig.model_validate(draft()))
    io = ScriptedIO(['2', 'kept-draft', '1', 'q', '0', '1', 's', '1'])
    assert run_wizard(tmp_path, section='models', **io.options()) == 0
    assert load_config(tmp_path).models[0].id == 'kept-draft'
    assert any('kept-draft' in line for line in io.messages)


def test_no_dirty_return_needs_no_confirmation(tmp_path, no_actions):
    save_config(tmp_path, AppConfig.model_validate(draft()))
    io = ScriptedIO(['q'])
    assert run_wizard(tmp_path, section='server', **io.options()) == 1
    assert len(io.prompts) == 1


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_scoped_server_edits_without_touching_models(tmp_path, language, no_actions):
    data = draft()
    put_model(data, 'keep-me')
    save_config(tmp_path, AppConfig.model_validate(data))
    io = ScriptedIO(['2', '', '18444', '', 's', '1'], [''])
    assert run_wizard(tmp_path, ui=UIContext(language), section='server', **io.options()) == 0
    config = load_config(tmp_path)
    assert config.server.listen_port == 18444
    assert config.server.api_key == 'never-print-this-key'
    assert config.models[0].id == 'keep-me'
    assert 'never-print-this-key' not in '\n'.join(io.messages)


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_scoped_limits_retry_invalid_number_locally(tmp_path, language, no_actions):
    save_config(tmp_path, AppConfig.model_validate(draft()))
    io = ScriptedIO(['2', '2', '2', '-1', '7', 's', '1'])
    ui = UIContext(language)
    assert run_wizard(tmp_path, ui=ui, section='limits', **io.options()) == 0
    assert load_config(tmp_path).logging.backup_count == 7
    assert any(ui.t(MESSAGES, 'backup_count') in line for line in io.messages)


def test_choices_retry_bad_values_without_leaving_form(tmp_path):
    io = ScriptedIO(['bad', '', '-1', '500', '9' * 5000, '2'])
    wizard = Wizard(tmp_path, **io.options())
    assert wizard.choose('Choice', [('a', 'A'), ('b', 'B')]) == 'b'
    assert len(io.prompts) == 6


@pytest.mark.parametrize('answer,expected', [('Y', True), ('y', True), ('1', True), ('N', False), ('n', False), ('0', False), ('', False)])
def test_confirmation_aliases(tmp_path, answer, expected):
    io = ScriptedIO(['invalid', answer])
    assert Wizard(tmp_path, **io.options()).confirm('confirm_save') is expected


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_bilingual_diagnostics_never_include_input_context_or_path(language):
    ui = UIContext(language)
    value = draft()
    value['server']['api_key'] = 'VERY PRIVATE SECRET'
    with pytest.raises(ValueError) as caught:
        AppConfig.model_validate(value)
    result = diagnostic(caught.value, ui)
    assert 'server.api_key' in result
    assert 'PRIVATE' not in result
    assert 'secret-path' not in diagnostic(FileNotFoundError('secret-path'), ui)
    assert diagnostic(ValueError('duplicate_model'), ui) == ui.t(MESSAGES, 'duplicate_model')
    assert 'untrusted-error' not in diagnostic(RuntimeError('untrusted-error'), ui)


def test_shared_ui_context_switches_existing_wizard_without_global_state(tmp_path):
    ui = UIContext('en-US')
    wizard = Wizard(tmp_path, ui=ui)
    english = wizard.t('models_title')
    ui.set_language('zh-CN')
    assert wizard.t('models_title') != english
    assert Wizard(tmp_path).t('models_title') == english


def test_invalid_existing_original_survives_cancel_then_backed_up_on_save(tmp_path, no_actions):
    original = b'{"broken":"SECRET VALUE"}'
    path = tmp_path / 'config.json'
    path.write_bytes(original)
    io = ScriptedIO(['', '', '', 'q', '2'], ['manual-key'])
    assert run_wizard(tmp_path, **io.options()) == 1
    assert path.read_bytes() == original
    assert 'SECRET VALUE' not in '\n'.join(io.messages)
    io = ScriptedIO(['', '', '', 's', '1'], ['manual-key'])
    assert run_wizard(tmp_path, **io.options()) == 0
    assert (tmp_path / 'config.json.bak').read_bytes() == original


def test_eof_discards_unsaved_draft_without_actions(tmp_path, no_actions):
    save_config(tmp_path, AppConfig.model_validate(draft()))
    original = (tmp_path / 'config.json').read_bytes()
    answers = iter(['2', 'unsaved', '1'])
    def ask(_):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError()
    assert run_wizard(tmp_path, section='models', ask=ask, output=lambda _: None) == 1
    assert (tmp_path / 'config.json').read_bytes() == original


def test_scoped_preset_import_and_delete_registration_only(tmp_path, no_actions):
    data = draft()
    put_model(data, 'm')
    save_config(tmp_path, AppConfig.model_validate(data))
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(graph()))
    io = ScriptedIO(['2', '1', 'v', str(source), '1', '3', '2', '1', 's', '1'])
    assert run_wizard(tmp_path, section='presets', **io.options()) == 0
    template = tmp_path / load_config(tmp_path).presets[0].template
    io = ScriptedIO(['4', '1', '1', 's', '1'])
    assert run_wizard(tmp_path, section='presets', **io.options()) == 0
    assert load_config(tmp_path).presets == []
    assert template.exists()


def test_external_menu_labels_are_terminal_safe_and_markup_literal(tmp_path):
    io = ScriptedIO(['1'])
    wizard = Wizard(tmp_path, **io.options())
    assert wizard.choose('Safe title', [('id', '\x1b[31m[bold]external\x00')]) == 'id'
    rendered = '\n'.join(io.messages)
    assert '\x1b' not in rendered and '\x00' not in rendered
    assert '[bold]external' in rendered


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_invalid_save_preserves_model_and_preset_draft_for_correction(tmp_path, language, no_actions):
    data = draft()
    put_model(data, 'm')
    put_preset(tmp_path, data, graph(), model='m', voice='v',
               text_binding={'node_id': '2', 'field': 'text'}, audio_output_node='3')
    save_config(tmp_path, AppConfig.model_validate(data))
    # Enabling keep without a binding makes the whole draft invalid. Failed save
    # must retain the rename; revert only the policy, then save that retained draft.
    io = ScriptedIO(['3', '1', 'renamed', '2', 's', '3', '1', '', '1', 's', '1'])
    ui = UIContext(language)
    assert run_wizard(tmp_path, ui=ui, section='models', **io.options()) == 0
    saved = load_config(tmp_path)
    assert saved.models[0].id == 'renamed'
    assert saved.models[0].cache_policy == 'inherit'
    assert saved.presets[0].model == 'renamed'
    assert ui.t(MESSAGES, 'not_saved') in io.messages


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('interruption', [EOFError, KeyboardInterrupt])
def test_first_setup_interruption_never_saves_or_starts(tmp_path, language, interruption, no_actions):
    def interrupted(_):
        raise interruption()
    assert run_wizard(tmp_path, ui=UIContext(language), first_run=True,
                      secret=interrupted, ask=lambda _: pytest.fail('no later prompt'), output=lambda _: None) == 1
    assert not (tmp_path / 'config.json').exists()
    assert not (tmp_path / 'runtime/control.json').exists()
    assert not (tmp_path / 'runtime/tasks.sqlite3').exists()


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_first_setup_model_picker_cancel_defers_safely(tmp_path, language, no_actions):
    io = ScriptedIO(['', '', '', '1', 'not-registered', '0', '1'], ['manual-key'])
    assert run_wizard(tmp_path, ui=UIContext(language), first_run=True, **io.options()) == 0
    assert load_config(tmp_path).models == []
    assert load_config(tmp_path).presets == []


def test_node_candidate_labels_sanitized_without_dumping_graph_text(tmp_path):
    document = graph()
    document['2']['_meta'] = {'title': '\x1b[31m[bold]Title\n\x00'}
    document['2']['class_type'] = '\x1b[32mIndexTTS25BaseNode'
    io = ScriptedIO(['1'])
    wizard = Wizard(tmp_path, **io.options())
    selected = wizard.choose(wizard.t('select_text'), binding_choices(document))
    assert selected == {'node_id': '2', 'field': 'text'}
    assert all('\x1b' not in line and '\x00' not in line and '\n' not in line for line in io.messages)
    assert 'private-template-text' not in '\n'.join(io.messages)


def test_default_output_keeps_external_rich_markup_literal(tmp_path, capsys):
    wizard = Wizard(tmp_path, ask=lambda _: '1')
    assert wizard.choose('Choice', [('value', '[red]literal[/red]')]) == 'value'
    assert '[red]literal[/red]' in capsys.readouterr().out


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_blank_confirmation_cancels_delete_and_save_by_default(tmp_path, language, no_actions):
    data = draft()
    put_model(data, 'keep-me')
    save_config(tmp_path, AppConfig.model_validate(data))
    original = (tmp_path / 'config.json').read_bytes()
    io = ScriptedIO(['4', '1', '', 's', '', 'q'])
    ui = UIContext(language)
    assert run_wizard(tmp_path, ui=ui, section='models', **io.options()) == 1
    assert (tmp_path / 'config.json').read_bytes() == original
    assert not (tmp_path / 'config.json.bak').exists()
    assert '[0] ' + ui.t(MESSAGES, 'confirm_no') in io.messages
    assert '默认' in ui.t(MESSAGES, 'confirm_no') if language == 'zh-CN' else 'default' in ui.t(MESSAGES, 'confirm_no')


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_first_run_blank_optional_and_save_confirmations_do_not_save(tmp_path, language, no_actions):
    io = ScriptedIO(['', '', '', '', '', 'q', '2'], ['manual-key'])
    assert run_wizard(tmp_path, ui=UIContext(language), first_run=True, **io.options()) == 1
    assert not (tmp_path / 'config.json').exists()


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('scope', ['root', 'server', 'model'])
def test_diagnostic_unknown_extra_field_never_echoes_key_or_value(language, scope):
    value = draft()
    if scope == 'root':
        target = value
    elif scope == 'server':
        target = value['server']
    else:
        value['models'] = [{'id': 'm'}]
        target = value['models'][0]
    secret_field = 'synthetic-private-key-token'
    target[secret_field] = 'synthetic-private-value'
    with pytest.raises(ValueError) as caught:
        AppConfig.model_validate(value)
    result = diagnostic(caught.value, UIContext(language))
    assert secret_field not in result
    assert 'synthetic-private-value' not in result
    assert 'configuration: extra_forbidden' in result


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_diagnostic_other_error_types_also_redact_unknown_location(language):
    from pydantic import ValidationError
    error = ValidationError.from_exception_data('Synthetic', [
        {'type': 'string_type', 'loc': ('server', 'synthetic-private-key-token'), 'input': 'private-value'}])
    result = diagnostic(error, UIContext(language))
    assert 'synthetic-private-key-token' not in result
    assert 'private-value' not in result
    assert 'configuration: string_type' in result


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('location', [('server', 'api_key'), ('presets', 0, 'text_binding', 'node_id')])
def test_diagnostic_trusted_schema_locations_and_indices_remain_useful(language, location):
    from pydantic import ValidationError
    error = ValidationError.from_exception_data('Synthetic', [
        {'type': 'string_type', 'loc': location, 'input': 'private-value'}])
    result = diagnostic(error, UIContext(language))
    assert '.'.join(map(str, location)) in result
    assert 'private-value' not in result


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_repair_editor_does_not_echo_unknown_ascii_extra_key(tmp_path, language, no_actions):
    value = draft()
    value['synthetic-private-key-token'] = 'synthetic-private-value'
    original = json.dumps(value).encode('utf-8')
    (tmp_path / 'config.json').write_bytes(original)
    io = ScriptedIO(['', '', '', 'q', '2'], ['manual-key'])
    assert run_wizard(tmp_path, ui=UIContext(language), **io.options()) == 1
    displayed = '\n'.join(io.messages + io.prompts)
    assert 'synthetic-private-key-token' not in displayed
    assert 'synthetic-private-value' not in displayed
    assert 'never-print-this-key' not in displayed
    assert (tmp_path / 'config.json').read_bytes() == original


def test_put_model_idle_settings_round_trip_rename_and_switch_away():
    data = draft()
    idle = {'seconds': 901, 'method': 'workflow', 'text': 'private-cleanup-text'}
    put_model(data, 'm', 'idle_release', idle_release=idle)
    idle['seconds'] = 1
    assert data['models'][0]['idle_release']['seconds'] == 901
    put_model(data, 'renamed', 'idle_release', replace='m')
    assert data['models'][0]['idle_release'] == {'seconds': 901, 'method': 'workflow', 'text': 'private-cleanup-text'}
    previous = copy.deepcopy(data)
    with pytest.raises(ValueError):
        put_model(data, 'renamed', 'idle_release', replace='renamed', idle_release={'seconds': 0, 'method': 'free'})
    assert data == previous
    put_model(data, 'renamed', 'inherit', replace='renamed')
    assert 'idle_release' not in data['models'][0]


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('method,number', [('free', '1'), ('workflow', '2')])
def test_fourth_strategy_full_bilingual_save_without_remote_calls(tmp_path, language, method, number, no_actions, monkeypatch):
    import httpx
    def no_http(*args, **kwargs):
        pytest.fail('configuration must not contact ComfyUI')
    monkeypatch.setattr(httpx.Client, 'request', no_http)
    monkeypatch.setattr(httpx.AsyncClient, 'request', no_http)
    save_config(tmp_path, AppConfig.model_validate(draft()))
    answers = ['2', 'm', '4', '', number, '1']
    if method == 'workflow':
        answers.append('private-cleanup-text')
    answers.extend(['s', '1'])
    io = ScriptedIO(answers)
    ui = UIContext(language)
    assert run_wizard(tmp_path, ui=ui, section='models', **io.options()) == 0
    model = load_config(tmp_path).models[0]
    assert model.cache_policy == 'idle_release'
    assert model.idle_release.seconds == 300 and model.idle_release.method == method
    assert model.idle_release.text == ('private-cleanup-text' if method == 'workflow' else '好。')
    assert '[4] ' + ui.t(MESSAGES, 'idle_release') in io.messages
    for index, key in enumerate(('inherit', 'keep', 'release'), 1):
        assert f'[{index}] ' + ui.t(MESSAGES, key) in io.messages
    assert ui.t(MESSAGES, 'idle_' + method + '_warning') in io.messages
    rendered = '\n'.join(io.messages)
    assert 'private-cleanup-text' not in rendered
    assert 'never-print-this-key' not in rendered
    if method == 'free':
        assert ('其他工作流' if language == 'zh-CN' else 'other workflows') in rendered
        assert ('无法锁定其他客户端' if language == 'zh-CN' else 'cannot lock other clients') in rendered
        assert ('队列为空' if language == 'zh-CN' else 'queue is empty') in rendered
    else:
        assert ('真实 GPU' if language == 'zh-CN' else 'real GPU') in rendered
        assert ('音频产物' if language == 'zh-CN' else 'audio artifacts') in rendered
        assert ('最后一次提交' if language == 'zh-CN' else 'last submitted') in rendered
        assert ('共享可释放的缓存' if language == 'zh-CN' else 'share a releasable cache') in rendered


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('method', ['1', '2'])
@pytest.mark.parametrize('answer', ['', '0', 'n'])
def test_idle_warning_default_cancel_leaves_existing_model_untouched(tmp_path, language, method, answer):
    data = draft()
    put_model(data, 'original', 'keep')
    original = copy.deepcopy(data)
    io = ScriptedIO(['1', 'renamed', '4', '300', method, answer])
    Wizard(tmp_path, ui=UIContext(language), **io.options()).model(data, edit=True)
    assert data == original


@pytest.mark.parametrize('bad_seconds', ['0', '86401', '-1', '1.2', 'true', 'not-a-number'])
def test_idle_seconds_invalid_values_retry_locally(tmp_path, bad_seconds):
    data = draft()
    io = ScriptedIO(['m', '4', bad_seconds, '86400', '1', '1'])
    Wizard(tmp_path, **io.options()).model(data)
    assert data['models'][0]['idle_release']['seconds'] == 86400
    assert sum('Idle timeout' in prompt for prompt in io.prompts) == 2


@pytest.mark.parametrize('method,number', [('free', '1'), ('workflow', '2')])
def test_editing_unchanged_idle_policy_preserves_custom_settings(tmp_path, method, number):
    data = draft()
    put_model(data, 'm', 'idle_release', idle_release={'method': method, 'seconds': 1234, 'text': 'preserve-this-text'})
    answers = ['1', '', '4', '', number, '1'] + ([''] if method == 'workflow' else [])
    io = ScriptedIO(answers)
    Wizard(tmp_path, **io.options()).model(data, edit=True)
    assert data['models'][0]['idle_release'] == {'method': method, 'seconds': 1234, 'text': 'preserve-this-text'}


@pytest.mark.parametrize('policy,number', [('inherit', '1'), ('keep', '2'), ('release', '3')])
def test_menu_switching_away_removes_idle_settings(tmp_path, policy, number):
    data = draft()
    put_model(data, 'm', 'idle_release', idle_release={'method': 'free'})
    io = ScriptedIO(['1', '', number])
    Wizard(tmp_path, **io.options()).model(data, edit=True)
    assert data['models'][0] == {'id': 'm', 'cache_policy': policy}


def test_method_selection_is_explicit_and_workflow_text_retries(tmp_path):
    data = draft()
    io = ScriptedIO(['m', '4', '1', '', '3', '2', '1', 'x' * 65, 'new short text'])
    Wizard(tmp_path, **io.options()).model(data)
    idle = data['models'][0]['idle_release']
    assert idle == {'seconds': 1, 'method': 'workflow', 'text': 'new short text'}
    assert sum('no automatic default' in prompt for prompt in io.prompts) == 4  # cache once, method three times


def test_idle_free_preset_can_omit_cache_binding(tmp_path):
    data = draft()
    put_model(data, 'm', 'idle_release', idle_release={'method': 'free'})
    preset = put_preset(tmp_path, data, graph(), model='m', voice='v',
                        text_binding={'node_id': '2', 'field': 'text'}, audio_output_node='3')
    assert preset['cache_binding'] is None
    validate_draft(tmp_path, data)
    for language in ('zh-CN', 'en-US'):
        assert '/free' in UIContext(language).t(MESSAGES, 'no_cache')


def test_idle_workflow_preset_requires_cache_binding(tmp_path):
    data = draft()
    put_model(data, 'm', 'idle_release', idle_release={'method': 'workflow'})
    with pytest.raises(ValueError):
        put_preset(tmp_path, data, graph(), model='m', voice='v',
                   text_binding={'node_id': '2', 'field': 'text'}, audio_output_node='3')
    put_preset(tmp_path, data, graph(), model='m', voice='v', text_binding={'node_id': '2', 'field': 'text'},
               audio_output_node='3', cache_binding={'node_id': '1', 'field': 'keep_models_cached'})
    validate_draft(tmp_path, data)


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_idle_diagnostic_fields_are_known_but_unknown_codes_and_text_are_not(language):
    from pydantic import ValidationError
    from comfybridge.workflows import WorkflowError
    from comfybridge.config import ConfigError
    ui = UIContext(language)
    for field in ('seconds', 'method', 'text'):
        error = ValidationError.from_exception_data('Synthetic', [
            {'type': 'string_type', 'loc': ('models', 0, 'idle_release', field), 'input': 'SECRET-CLEANUP-TEXT'}])
        result = diagnostic(error, ui)
        assert f'models.0.idle_release.{field}' in result
        assert 'SECRET-CLEANUP-TEXT' not in result
    for error in (WorkflowError('synthetic_private_token'), ConfigError('synthetic_private_token')):
        assert 'synthetic_private_token' not in diagnostic(error, ui)
    for code in ('cache_cleanup_unsupported', 'cache_cleanup_seed_binding', 'cache_cleanup_seed_schema', 'cache_cleanup_seed_range'):
        assert diagnostic(WorkflowError(code), ui) == ui.t(MESSAGES, code)


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_idle_summary_hides_cleanup_text_and_malformed_seconds(tmp_path, language):
    data = draft()
    put_model(data, 'm', 'idle_release', idle_release={'method': 'workflow', 'seconds': 444, 'text': 'PRIVATE-TEXT'})
    io = ScriptedIO([])
    wizard = Wizard(tmp_path, ui=UIContext(language), **io.options())
    wizard.show_summary(data, 'models')
    assert '444' in '\n'.join(io.messages)
    assert 'PRIVATE-TEXT' not in '\n'.join(io.messages)
    io.messages.clear()
    data['models'][0]['idle_release']['seconds'] = 'PRIVATE-SECONDS'
    data['models'][0]['idle_release']['method'] = 'PRIVATE-METHOD'
    wizard.show_summary(data, 'models')
    assert 'PRIVATE' not in '\n'.join(io.messages)


def test_invalid_new_model_does_not_even_add_empty_models_collection():
    data = {}
    with pytest.raises(ValueError):
        put_model(data, 'invalid/model')
    assert data == {}
    with pytest.raises(ValueError):
        put_model(data, 'm', 'idle_release', idle_release={'method': 'free', 'seconds': 0})
    assert data == {}


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_unknown_pydantic_error_type_is_not_echoed(language):
    from pydantic import ValidationError
    from pydantic_core import PydanticCustomError
    error = ValidationError.from_exception_data('Synthetic', [{
        'type': PydanticCustomError('synthetic_private_type', 'private-message'),
        'loc': ('models', 0, 'idle_release', 'text'), 'input': 'private-cleanup-text'}])
    result = diagnostic(error, UIContext(language))
    assert 'synthetic_private_type' not in result
    assert 'private-message' not in result and 'private-cleanup-text' not in result
    assert 'models.0.idle_release.text: invalid_value' in result
