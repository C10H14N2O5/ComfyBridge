"""Small explicit bilingual catalogs and terminal-safe external labels.

There is no global language state. Renderers must still disable Rich markup and
highlighting: square brackets are legitimate identifier/path characters here.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from string import Formatter
import unicodedata

LANGUAGES = ("zh-CN", "en-US")
_FORMATTER = Formatter()
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)


def safe_text(value: object, max_length: int = 1024) -> str:
    """Return a bounded one-line label without ANSI, controls or bidi overrides.

    CSI and terminal string payloads (including OSC hyperlinks/clipboard commands)
    are removed as complete sequences, not merely by deleting their ESC byte.
    An unterminated terminal string is discarded through the end of the value.
    Ordinary Unicode, paths and IDs are not translated or normalized.
    """
    if type(max_length) is not int or max_length < 0:
        raise ValueError("invalid_text_limit")
    if max_length == 0:
        return ""
    try:
        text = str(value)
    except Exception:
        return "<unavailable>"[:max_length]
    result: list[str] = []
    index = 0
    while index < len(text) and len(result) < max_length:
        char = text[index]
        index += 1
        sequence = None
        if char == "\x1b":
            if index == len(text):
                break
            sequence = text[index]
            index += 1
        elif char in ("\x9b", "\x9d", "\x90", "\x98", "\x9e", "\x9f"):
            sequence = {"\x9b": "[", "\x9d": "]", "\x90": "P", "\x98": "X",
                        "\x9e": "^", "\x9f": "_"}[char]
        if sequence == "[":
            while index < len(text):
                code = ord(text[index])
                index += 1
                if 0x40 <= code <= 0x7E:
                    break
            continue
        if sequence in ("]", "P", "X", "^", "_"):
            while index < len(text):
                if text[index] == "\x9c" or (sequence == "]" and text[index] == "\x07"):
                    index += 1
                    break
                if text[index:index + 2] == "\x1b\\":
                    index += 2
                    break
                index += 1
            continue
        if sequence is not None:
            # Other ESC sequences have zero or more intermediate bytes then a final.
            while 0x20 <= ord(sequence) <= 0x2F and index < len(text):
                sequence = text[index]
                index += 1
            continue
        if unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            continue
        result.append(char)
    return "".join(result)


def _placeholders(message: str) -> set[tuple[str, str, str | None]]:
    signatures = set()
    try:
        for _, field, spec, conversion in _FORMATTER.parse(message):
            if field is None:
                continue
            # Named static fields only: no attribute/index traversal, positional
            # substitutions or dynamic nested format expressions.
            if (not _FIELD.fullmatch(field) or "{" in spec or "}" in spec
                    or conversion not in (None, "s", "r", "a")):
                raise ValueError
            signatures.add((field, spec, conversion))
    except (ValueError, TypeError):
        raise ValueError("invalid_translation_placeholder") from None
    return signatures


def validate_catalog(catalog: Mapping[str, tuple[str, str]]) -> None:
    """Reject malformed pairs and mismatched static formatting placeholders."""
    if not isinstance(catalog, Mapping):
        raise ValueError("invalid_translation_catalog")
    for key, pair in catalog.items():
        if (type(key) is not str or not key or not isinstance(pair, (tuple, list))
                or len(pair) != 2 or any(type(text) is not str for text in pair)):
            raise ValueError("invalid_translation_catalog")
        if _placeholders(pair[0]) != _placeholders(pair[1]):
            raise ValueError("translation_placeholder_mismatch")


class UIContext:
    """Per-observer language choice; never changes business configuration."""
    def __init__(self, language: str = "en-US"):
        self.set_language(language)

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, language: str) -> None:
        if type(language) is not str or language not in LANGUAGES:
            raise ValueError("unsupported_ui_language")
        self._language = language

    def t(self, catalog: Mapping[str, tuple[str, str]], key: str, **values: object) -> str:
        try:
            pair = catalog[key]
        except (KeyError, TypeError):
            raise ValueError("unknown_translation_key") from None
        # Validate the selected pair every time; mutable module catalogs cannot leave
        # a stale validation cache. Full catalog parity is checked by module tests.
        validate_catalog({key: pair})
        template = pair[0 if self.language == "zh-CN" else 1]
        sanitized = {name: value if type(value) in (int, float, bool) else safe_text(value)
                     for name, value in values.items()}
        try:
            return template.format(**sanitized)
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ValueError("invalid_translation_values") from None
