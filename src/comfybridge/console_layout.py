"""Shared presentation layout for every console surface.

Standard library only (the offline Setup imports it too). Layout helpers build
logical lines from already-translated, already-sanitized text; they never add
translations or interpret Rich markup. Wrapping is presentation-only and is
applied exclusively by the default terminal outputs, never to redirected
streams or injected test outputs, so logical lines stay stable.

Layout rules
------------
* Page title: blank line, title, 48-character rule.
* Section: blank line, title without trailing colon; content indented 2,
  nested details indented 6.
* Key/value: ``  label  value`` padded to the widest label (display width).
* Bullet: ``  - text``. Multi-line catalog templates use ``\\n`` and write
  bullet lines literally.
* Menu options stay at column 0 (``[n] label``); prompts follow on their own.
* Confirmation: blank line, warning lines, ``[1] ...``, ``[0] ...``.
* Result feedback: blank line, then the message at column 0.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import re
import shutil
import sys
import unicodedata

RULE_WIDTH = 48
MAX_WIDTH = 100
MIN_WIDTH = 40

_OPTION = re.compile(r'\[[^\]\s][^\]]{0,15}\] ')
_KV = re.compile(r'(\S(?:.*?\S)?)( {2,})(?=\S)')


def display_width(text: str) -> int:
    """Terminal cell width: East Asian wide/fullwidth count 2, combining 0."""
    width = 0
    for char in text:
        if unicodedata.combining(char) or unicodedata.category(char) in ('Mn', 'Me', 'Cf'):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return width


def split_lines(text: str) -> list[str]:
    return str(text).split('\n')


def rule() -> str:
    return '-' * RULE_WIDTH


def title_block(title: str) -> list[str]:
    return ['', title, rule()]


def section(title: str) -> list[str]:
    return ['', title]


def kv_lines(rows: Iterable[tuple[str, str]], indent: int = 2, gap: int = 2) -> list[str]:
    rows = [(str(label), str(value)) for label, value in rows]
    if not rows:
        return []
    widest = max(display_width(label) for label, _ in rows)
    prefix = ' ' * indent
    return [prefix + label + ' ' * (widest - display_width(label) + gap) + value for label, value in rows]


def bullets(items: Iterable[str], indent: int = 2) -> list[str]:
    return [' ' * indent + '- ' + str(item) for item in items]


def indent_lines(lines: Iterable[str], count: int) -> list[str]:
    prefix = ' ' * count
    return [prefix + line if line else line for line in lines]


def confirm_lines(message: str, yes: str, no: str) -> list[str]:
    return ['', *split_lines(message), yes, no]


def emit(output: Callable[[str], None], text: str) -> None:
    for line in split_lines(text):
        output(line)


def _continuation_indent(line: str, width: int) -> int:
    stripped = line.lstrip(' ')
    leading = len(line) - len(stripped)
    if stripped.startswith('- '):
        return leading + 2
    option = _OPTION.match(stripped)
    if option:
        return leading + display_width(option.group(0))
    pair = _KV.match(stripped)
    if pair:
        column = leading + display_width(pair.group(1)) + len(pair.group(2))
        if column <= width // 2:
            return column
    return leading


def _tokens(text: str) -> list[str]:
    """Words, single spaces and single wide characters (breakable anywhere)."""
    tokens: list[str] = []
    word = ''
    for char in text:
        if char == ' ':
            if word:
                tokens.append(word)
                word = ''
            tokens.append(' ')
        elif unicodedata.east_asian_width(char) in ('W', 'F'):
            if word:
                tokens.append(word)
                word = ''
            tokens.append(char)
        else:
            word += char
    if word:
        tokens.append(word)
    return tokens


def wrap_line(line: str, width: int) -> list[str]:
    """Wrap one logical line with a hanging indent; never splits wide chars."""
    if width <= 0 or display_width(line) <= width:
        return [line]
    hang = min(_continuation_indent(line, width), max(0, width - 10))
    stripped = line.lstrip(' ')
    # Leave room for at least one wide character even if a caller supplies an
    # indent wider than the viewport. Empty/whitespace-only lines stay blank.
    if not stripped:
        return ['']
    first_prefix = ' ' * min(len(line) - len(stripped), max(0, width - 2))
    result: list[str] = []
    current, current_width = first_prefix, display_width(first_prefix)
    content = False  # whether `current` holds non-indent text
    for token in _tokens(stripped):
        token_width = display_width(token)
        if token == ' ':
            if content and current_width + 1 <= width:
                current += ' '
                current_width += 1
            continue
        if content and current_width + token_width > width:
            result.append(current.rstrip(' '))
            current, current_width, content = ' ' * hang, hang, False
        while current_width + token_width > width and len(token) > 1:
            # A single overlong word (for example a path) is hard-broken.
            room = width - current_width
            piece, piece_width = '', 0
            for char in token:
                char_width = display_width(char)
                if piece and piece_width + char_width > room:
                    break
                piece += char
                piece_width += char_width
            result.append(current + piece)
            token = token[len(piece):]
            token_width = display_width(token)
            current, current_width, content = ' ' * hang, hang, False
        current += token
        current_width += token_width
        content = True
    if content or not result:
        result.append(current.rstrip(' '))
    return result


def terminal_columns(is_tty: bool, columns: int | None) -> int | None:
    """Wrapping width for a real terminal, or None (no wrapping) otherwise."""
    if not is_tty or type(columns) is not int or columns <= 0:
        return None
    return max(MIN_WIDTH, min(columns - 1, MAX_WIDTH))


def default_style(line: str) -> str | None:
    stripped = line.lstrip()
    if stripped.startswith('ComfyBridge '):
        return 'bold cyan'
    if stripped.startswith(('[0', '---')):
        return 'dim'
    if re.match(r'\[[1-9A-Z]', stripped):
        return 'cyan'
    return None


def rich_output(console=None, style: Callable[[str], str | None] = default_style) -> Callable[[str], None]:
    """Rich renderer with markup/highlighting disabled and our own wrapping."""
    if console is None:
        from rich.console import Console
        console = Console()

    def output(text: str) -> None:
        width = terminal_columns(bool(getattr(console, 'is_terminal', False)), getattr(console, 'width', None))
        for line in split_lines(text):
            line_style = style(line) if style else None
            for piece in (wrap_line(line, width) if width else [line]):
                console.print(piece, style=line_style, markup=False, highlight=False, soft_wrap=True)
    return output


def _stream_columns(stream) -> int | None:
    try:
        is_tty = stream is not None and stream.isatty()
    except (OSError, ValueError, AttributeError):
        return None
    return terminal_columns(is_tty, shutil.get_terminal_size((MAX_WIDTH + 1, 24)).columns if is_tty else None)


def plain_output(stream=None) -> Callable[[str], None]:
    """print-based renderer (no Rich); wraps only on an interactive terminal."""
    def output(text: str) -> None:
        target = stream if stream is not None else sys.stdout
        width = _stream_columns(target)
        for line in split_lines(text):
            for piece in (wrap_line(line, width) if width else [line]):
                print(piece, file=target)
    return output
