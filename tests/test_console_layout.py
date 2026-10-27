import io

import pytest

from comfybridge import console_layout as layout


def test_display_width_counts_wide_and_combining_characters():
    assert layout.display_width('abc') == 3
    assert layout.display_width('语音服务') == 8
    assert layout.display_width('ComfyUI 连接') == 12
    assert layout.display_width('e\u0301') == 1
    assert layout.display_width('：') == 2


def test_blocks_and_kv_alignment_use_display_width():
    assert layout.title_block('标题') == ['', '标题', '-' * 48]
    assert layout.section('任务') == ['', '任务']
    lines = layout.kv_lines([('后台', '已验证连接'), ('ComfyUI 连接', '在线'), ('语音服务就绪', '是')])
    assert lines == ['  后台          已验证连接', '  ComfyUI 连接  在线', '  语音服务就绪  是']
    assert layout.kv_lines([]) == []
    assert layout.bullets(['a', 'b'], indent=4) == ['    - a', '    - b']
    assert layout.indent_lines(['a', '', 'b'], 2) == ['  a', '', '  b']


def test_confirm_lines_and_emit_split_templates():
    assert layout.confirm_lines('确认？\n  - 风险', '[1] 确认', '[0] 取消（默认）') == [
        '', '确认？', '  - 风险', '[1] 确认', '[0] 取消（默认）']
    lines = []
    layout.emit(lines.append, 'first\nsecond')
    assert lines == ['first', 'second']


def test_short_lines_are_unchanged():
    assert layout.wrap_line('short', 40) == ['short']
    assert layout.wrap_line('x' * 200, 0) == ['x' * 200]


def test_bullet_continuation_aligns_after_marker():
    line = '  - ' + 'word ' * 20
    wrapped = layout.wrap_line(line.rstrip(), 40)
    assert len(wrapped) > 1
    assert all(layout.display_width(piece) <= 40 for piece in wrapped)
    assert all(piece.startswith('    ') and not piece.startswith('     ') for piece in wrapped[1:])
    assert ' '.join(' '.join(wrapped).split()) == ' '.join(line.split())


def test_option_and_kv_continuations_align_to_content():
    option = layout.wrap_line('[5 / F] ' + '强制停止' * 12, 40)
    assert all(piece.startswith(' ' * 8) for piece in option[1:])
    pair = layout.wrap_line('  后台  ' + '无法确认' * 12, 40)
    assert all(piece.startswith(' ' * 8) and piece[8] != ' ' for piece in pair[1:])
    assert all(layout.display_width(piece) <= 40 for piece in option + pair)


def test_cjk_breaks_between_characters_and_paths_hard_break():
    text = '中' * 50
    wrapped = layout.wrap_line(text, 41)
    assert ''.join(wrapped) == text
    assert all(layout.display_width(piece) <= 41 for piece in wrapped)
    path = 'C:\\' + 'a' * 120
    wrapped = layout.wrap_line(path, 40)
    assert ''.join(wrapped) == path
    assert all(layout.display_width(piece) <= 40 for piece in wrapped)


def test_excessive_indentation_never_exceeds_viewport():
    wrapped = layout.wrap_line(' ' * 60 + '中' * 30, 40)
    assert all(layout.display_width(line) <= 40 for line in wrapped)
    assert ''.join(line.strip() for line in wrapped) == '中' * 30
    assert layout.wrap_line(' ' * 60, 40) == ['']


@pytest.mark.parametrize('is_tty,columns,expected', [(False, 200, None), (True, 200, 100), (True, 60, 59),
                                                     (True, 10, 40), (True, None, None), (True, 0, None)])
def test_terminal_columns_wraps_only_real_terminals(is_tty, columns, expected):
    assert layout.terminal_columns(is_tty, columns) == expected


def test_plain_output_never_wraps_redirected_streams():
    stream = io.StringIO()
    layout.plain_output(stream)('x ' * 200 + '\nnext')
    assert stream.getvalue() == 'x ' * 200 + '\nnext\n'


def test_plain_output_wraps_terminal(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(layout.shutil, 'get_terminal_size', lambda fallback: type('S', (), {'columns': 41})())
    stream = Terminal()
    layout.plain_output(stream)('  - ' + 'word ' * 20)
    lines = stream.getvalue().splitlines()
    assert len(lines) > 1 and all(layout.display_width(line) <= 40 for line in lines)


@pytest.mark.parametrize('terminal', [False, True])
def test_rich_output_disables_markup_and_highlighting(terminal):
    calls = []
    class Console:
        is_terminal = terminal
        width = 41
        def print(self, text, **kwargs):
            calls.append((text, kwargs))
    layout.rich_output(Console())('[red]literal[/red] ' + 'word ' * 20 + '\n[1] option')
    assert calls and all(kwargs['markup'] is False and kwargs['highlight'] is False and kwargs['soft_wrap'] is True
                         for _, kwargs in calls)
    assert calls[0][0].startswith('[red]literal[/red]')
    assert calls[-1] == ('[1] option', {'style': 'cyan', 'markup': False, 'highlight': False, 'soft_wrap': True})
    assert (len(calls) > 2) is terminal


def test_default_style_classifies_titles_options_and_rules():
    assert layout.default_style('ComfyBridge 0.4.1 — Main menu') == 'bold cyan'
    assert layout.default_style('[1] Start') == 'cyan'
    assert layout.default_style('[A] 语言 / Language') == 'cyan'
    assert layout.default_style('[0] Back') == 'dim'
    assert layout.default_style('[0 / Q] Close monitor') == 'dim'
    assert layout.default_style('-' * 48) == 'dim'
    assert layout.default_style('  后台  已验证连接') is None
