from types import MappingProxyType

import pytest

from comfybridge.i18n import UIContext, safe_text, validate_catalog


CATALOG = {
    "welcome": ("你好，{name}。", "Hello, {name}."),
    "count": ("任务：{count:d}；耗时：{seconds:.2f}", "Tasks: {count:d}; elapsed: {seconds:.2f}"),
    "menu": ("[1] 开始\n[0] 返回", "[1] Start\n[0] Back"),
    "literal": ("{{字段}}", "{{field}}"),
}


def test_english_default_and_independent_language_contexts():
    english = UIContext()
    chinese = UIContext("zh-CN")
    assert english.language == "en-US"
    assert english.t(CATALOG, "welcome", name="Example") == "Hello, Example."
    assert chinese.t(CATALOG, "welcome", name="Example") == "你好，Example。"
    chinese.set_language("en-US")
    assert chinese.t(CATALOG, "welcome", name="Example") == "Hello, Example."
    assert english.language == "en-US"


@pytest.mark.parametrize("language", ["zh", "en", "EN-US", "zh-TW", "", None, 1, ["en-US"]])
def test_invalid_language_never_changes_existing_context(language):
    ui = UIContext("zh-CN")
    with pytest.raises(ValueError, match="unsupported_ui_language"):
        ui.set_language(language)
    assert ui.language == "zh-CN"
    with pytest.raises(ValueError):
        UIContext(language)


def test_catalog_parity_accepts_static_named_formats_and_mapping():
    validate_catalog(MappingProxyType(CATALOG))
    assert UIContext().t(CATALOG, "count", count=3, seconds=1.125) == "Tasks: 3; elapsed: 1.12"
    assert UIContext().t(CATALOG, "literal") == "{field}"
    assert UIContext().t(CATALOG, "menu") == "[1] Start\n[0] Back"
    validate_catalog({"x": ("{a} {b} {a}", "{b} {a}")})


@pytest.mark.parametrize("catalog", [
    None, [], {"x": "two strings"}, {"x": ("中文",)}, {"x": ("中文", "English", "extra")},
    {"x": ("中文", None)}, {1: ("中文", "English")}, {"": ("中文", "English")},
    {"x": ("{value}", "{other}")}, {"x": ("{n:d}", "{n:.2f}")},
    {"x": ("{value!s}", "{value!r}")}, {"x": ("{", "{")},
    {"x": ("{}", "{}")}, {"x": ("{0}", "{0}")},
    {"x": ("{value.secret}", "{value.secret}")}, {"x": ("{value[0]}", "{value[0]}")},
    {"x": ("{value:{width}}", "{value:{width}}")}, {"x": ("{value!x}", "{value!x}")},
])
def test_invalid_catalog_is_rejected_without_echo(catalog):
    with pytest.raises(ValueError) as error:
        validate_catalog(catalog)
    assert str(error.value) in {
        "invalid_translation_catalog", "invalid_translation_placeholder", "translation_placeholder_mismatch",
    }


def test_translation_errors_do_not_echo_keys_or_values():
    ui = UIContext()
    with pytest.raises(ValueError, match="unknown_translation_key"):
        ui.t(CATALOG, "PRIVATE SECRET KEY")
    with pytest.raises(ValueError, match="invalid_translation_values"):
        ui.t(CATALOG, "welcome")
    with pytest.raises(ValueError, match="invalid_translation_values"):
        ui.t(CATALOG, "count", count="PRIVATE", seconds=2)


def test_mutated_catalog_is_revalidated():
    catalog = {"x": ("{name}", "{name}")}
    assert UIContext().t(catalog, "x", name="a") == "a"
    catalog["x"] = ("{name}", "{different}")
    with pytest.raises(ValueError, match="translation_placeholder_mismatch"):
        UIContext().t(catalog, "x", name="a")


@pytest.mark.parametrize("value,expected", [
    ("normal 中文 😀", "normal 中文 😀"),
    (r"C:\用户\Folder [1]\voice.json", r"C:\用户\Folder [1]\voice.json"),
    ("index-tts.2_5-Example", "index-tts.2_5-Example"),
    ("before\n\r\t\x00\x07\x08after", "beforeafter"),
    ("x\x1b[31mred\x1b[0my", "xredy"),
    ("x\x9b31mred\x9b0my", "xredy"),
    ("a\x1b[2Jb\x1b[Hc", "abc"),
    ("a\x1b]0;PRIVATE TITLE\x07b", "ab"),
    ("a\x1b]52;c;PRIVATE CLIPBOARD\x1b\\b", "ab"),
    ("a\x9d52;c;PRIVATE CLIPBOARD\x9cb", "ab"),
    ("\x1b]8;;https://secret.invalid\x07label\x1b]8;;\x07", "label"),
    ("a\x1bPPRIVATE DCS\x1b\\b", "ab"),
    ("a\x1b_PRIVATE APC\x1b\\b", "ab"),
    ("a\x1b^PRIVATE PM\x1b\\b", "ab"),
    ("a\x1bXPRIVATE SOS\x1b\\b", "ab"),
    ("a\x1b(Bb", "ab"),
    ("a\x1b7b", "ab"),
    ("a\x1b]0;unterminated payload", "a"),
    ("a\x1b[123;", "a"),
    ("a\x1b", "a"),
    ("safe\u202eevil\u2066label\u2069", "safeevillabel"),
    ("a\u200bb\u2028c\u2029d\ud800", "abcd"),
    (42, "42"), (None, "None"),
])
def test_safe_external_text(value, expected):
    assert safe_text(value) == expected


@pytest.mark.parametrize("limit", [0, 1, 5, 1024])
def test_safe_text_is_bounded(limit):
    assert safe_text("\x1b[31m" + "汉" * 2000 + "\x1b[0m", max_length=limit) == "汉" * limit


@pytest.mark.parametrize("limit", [-1, True, 1.5, "5"])
def test_safe_text_requires_nonnegative_integer_bound(limit):
    with pytest.raises(ValueError, match="invalid_text_limit"):
        safe_text("x", max_length=limit)


def test_bad_object_text_does_not_leak_exception():
    class Broken:
        def __str__(self):
            raise ValueError("PRIVATE SECRET")
    assert safe_text(Broken()) == "<unavailable>"
    assert safe_text(Broken(), 5) == "<unav"


def test_interpolation_sanitizes_dynamic_values_but_retains_static_newlines():
    catalog = {"x": ("第一行\n{name}", "First line\n{name}")}
    assert UIContext().t(catalog, "x", name="\x1b[31mExample\n\r\t\x1b[0m") == "First line\nExample"
    # Brackets are NOT Rich escaping: every renderer must set markup=False.
    assert UIContext().t(CATALOG, "welcome", name="[red]Example[/red]") == "Hello, [red]Example[/red]."


def test_numeric_subclasses_cannot_inject_terminal_formatting():
    class HostileInt(int):
        def __format__(self, spec):
            return "\x1b[2JPRIVATE"
    assert UIContext().t({"x": ("{value}", "{value}")}, "x", value=HostileInt(3)) == "3"
