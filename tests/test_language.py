"""Language setting: config round-trip and the per-clip English/Polish pick.

Run with ``python tests/test_language.py`` (no network, no model).
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dictate import cleanup, config  # noqa: E402


def test_detect_language():
    assert cleanup.detect_language("Hello, can you send me the invoice today?") == "en"
    assert cleanup.detect_language("I need to go to Warsaw") == "en"
    assert cleanup.detect_language("Cześć, czy możesz mi wysłać fakturę?") == "pl"
    # diacritics dropped by the engine: the word list still catches it
    assert cleanup.detect_language("no to jak juz mam to moze zrobimy tak") == "pl"


def test_resolve_language():
    assert cleanup.resolve_language("en", "Cześć, czy możesz") == "en"
    assert cleanup.resolve_language("pl", "Hello there") == "pl"
    assert cleanup.resolve_language("auto", "Cześć, czy możesz") == "pl"
    assert cleanup.resolve_language("nonsense", "Cześć") == "en"


def test_prompts_differ():
    assert "clean written Polish" in cleanup.system_prompt("x", "", "pl")
    assert "clean written English" in cleanup.system_prompt("x", "", "en")


def test_set_language_keeps_the_rest_of_the_file():
    path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# mine\ncleanup:\n  enabled: true\n  model: google/gemini-2.5-flash-lite\n  budget_s: 2.0\n")
    assert config.language(path) == "en"
    config.set_language("pl", path)
    assert config.language(path) == "pl"
    config.set_language("auto", path)
    data = config.load(path)
    assert data["language"] == "auto"
    assert data["cleanup"]["model"] == "google/gemini-2.5-flash-lite"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert text.startswith("# mine\n") and text.count("language:") == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
