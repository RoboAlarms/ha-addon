"""Every translation says the same things as strings.json: the same keys, the same placeholders."""

import json
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "roboalarms"
SOURCE = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
TRANSLATIONS = sorted((COMPONENT / "translations").glob("*.json"))
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def flatten(tree, prefix=""):
    """{"config.step.user.title": "...", ...} of a strings file."""
    out = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def test_english_is_the_source():
    """strings.json and translations/en.json stay identical (the rules in CLAUDE.md)."""
    english = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert english == SOURCE


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda p: p.stem)
def test_a_translation_matches_the_source(path):
    """No key missing or extra, every text translated, and the placeholders kept as they are."""
    source = flatten(SOURCE)
    translated = flatten(json.loads(path.read_text(encoding="utf-8")))
    assert sorted(translated) == sorted(source)
    for key, text in translated.items():
        assert isinstance(text, str) and text.strip(), key
        assert sorted(PLACEHOLDER.findall(text)) == sorted(PLACEHOLDER.findall(source[key])), key
