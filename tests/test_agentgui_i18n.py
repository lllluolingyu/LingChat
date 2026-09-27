"""Interface-language wiring for the AgentGUI browser UI.

The string table lives in JavaScript, so a typo'd key is invisible to every
Python test and shows up in the browser as a raw `app.new_chat` sitting where a
label should be. These tests read the table and its call sites as text and check
they agree.
"""

from __future__ import annotations

import re
from pathlib import Path

import agentgui

WEB = Path(agentgui.__file__).with_name("web")
I18N = WEB / "js" / "i18n.js"

# An entry starts at the table's own indent: `  "some.key": {`. Its body runs to
# the next entry, so a multi-line entry is captured whole without having to match
# braces. Option objects inside a body are indented further and never match.
_ENTRY_START = re.compile(r'^  "([a-z][\w.]*)": \{', re.M)


def _table(text: str) -> dict[str, str]:
    """Map each declared key to its entry body."""

    starts = list(_ENTRY_START.finditer(text))
    return {
        match.group(1): text[
            match.end() : (starts[i + 1].start() if i + 1 < len(starts) else len(text))
        ]
        for i, match in enumerate(starts)
    }


def _declared() -> dict[str, str]:
    entries = _table(I18N.read_text(encoding="utf-8"))
    assert entries, "no strings parsed out of i18n.js"
    return entries


def test_every_string_is_translated_into_every_language():
    for key, body in _declared().items():
        for lang in ("en", "zh"):
            assert f"{lang}:" in body, f"{key} has no {lang} translation"


def test_default_language_is_chinese():
    text = I18N.read_text(encoding="utf-8")
    assert 'export const DEFAULT_LANG = "zh"' in text
    # The pre-paint read decides which CJK face the font stacks fall through to,
    # so the page must set <html lang> before first paint rather than on boot.
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert "agentgui-lang" in index and "documentElement.lang" in index
    assert '<html lang="zh">' in index


def test_every_referenced_key_exists():
    declared = set(_declared())
    missing: list[str] = []
    for source in sorted((WEB / "js").glob("*.js")):
        text = source.read_text(encoding="utf-8")
        # `(?<![\w$])` keeps this off the tail of createElement("div").
        for key in re.findall(r'(?<![\w$])t\(\s*"([a-z][\w.]*)"', text):
            if key not in declared:
                missing.append(f"{source.name}: {key}")
    index = (WEB / "index.html").read_text(encoding="utf-8")
    for key in re.findall(r'data-i18n(?:-[a-z-]+)?="([a-z][\w.]*)"', index):
        if key not in declared:
            missing.append(f"index.html: {key}")
    assert not missing, f"keys used but never declared: {missing}"


def test_static_markup_carries_a_translation_for_every_data_i18n_node():
    """A `data-i18n` element must ship readable text, not an empty shell.

    The pass in i18n.js fills these in at boot, but the markup is what the
    browser paints first — an empty element there is a visible flash.
    """

    index = (WEB / "index.html").read_text(encoding="utf-8")
    for match in re.finditer(
        r'<(\w+)[^>]*\bdata-i18n="[a-z][\w.]*"[^>]*>(.*?)</\1>', index, re.S
    ):
        assert match.group(2).strip(), f"empty data-i18n element: {match.group(0)[:70]}"


def test_language_toggle_is_wired():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    main = (WEB / "js" / "main.js").read_text(encoding="utf-8")
    assert 'id="lang-toggle"' in index and 'id="lang-label"' in index
    assert "setLang(nextLang())" in main
    # A language change has to repaint what is already on screen, including the
    # transcript, whose tool and note labels were rendered in the old language.
    assert "onLangChange(" in main and "reloadTranscript()" in main
