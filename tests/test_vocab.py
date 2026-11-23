#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test vocabulary replacement engine.

Tests the vocab.py module with temporary fixtures.
"""

import sys
import tempfile
from pathlib import Path
import yaml
import os

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dictate.vocab import VocabEngine


def create_test_fixtures():
    """Create temporary YAML fixtures for testing."""
    temp_dir = Path(tempfile.mkdtemp())

    # Create dictionary.yaml
    # For vocabulary entries: phrase is the canonical form (how we want it to appear)
    # For entries with replacement: phrase is what was spoken, replacement is what to use
    dictionary = [
        {"phrase": "Alice", "frequency": 10},  # Canonical form for casing fix
        {"phrase": "Claude", "frequency": 8},  # Canonical form for casing fix
        {"phrase": "Supabase", "frequency": 5},  # Canonical form for casing fix
        {"phrase": "ExampleCo", "frequency": 3},  # Canonical form for casing fix
        {"phrase": "btw", "replacement": "by the way", "frequency": 2},  # Has replacement
    ]

    dict_path = temp_dir / "dictionary.yaml"
    with open(dict_path, "w", encoding="utf-8") as f:
        yaml.dump(dictionary, f)

    # Create snippets.yaml
    snippets = [
        {
            "trigger": "my email address",
            "text": "you@example.com",
        },
        {"trigger": "organize thoughts prompt", "text": "[organize thoughts prompt text here]"},
    ]

    snippets_path = temp_dir / "snippets.yaml"
    with open(snippets_path, "w", encoding="utf-8") as f:
        yaml.dump(snippets, f)

    return temp_dir, dict_path, snippets_path


def test_case_insensitive_replacement():
    """Test that replacement is case-insensitive but preserves canonical casing."""
    _, dict_path, snippets_path = create_test_fixtures()
    engine = VocabEngine(dict_path, snippets_path)

    # Test 1: "send it to alice and claude" -> "send it to Alice and Claude"
    result = engine.apply("send it to alice and claude")
    expected = "send it to Alice and Claude"
    assert result == expected, f"Expected '{expected}', got '{result}'"
    print(f"[PASS] Test 1: 'send it to alice and claude' -> '{result}'")

    # Test 2: "my email address" -> "you@example.com"
    result = engine.apply("my email address")
    expected = "you@example.com"
    assert result == expected, f"Expected '{expected}', got '{result}'"
    print(f"[PASS] Test 2: 'my email address' -> '{result}'")

    # Test 3: "btw" -> "by the way"
    result = engine.apply("btw this is good")
    expected = "by the way this is good"
    assert result == expected, f"Expected '{expected}', got '{result}'"
    print(f"[PASS] Test 3: 'btw this is good' -> '{result}'")


def test_word_not_in_list():
    """Test that words not in the list are untouched."""
    _, dict_path, snippets_path = create_test_fixtures()
    engine = VocabEngine(dict_path, snippets_path)

    result = engine.apply("hello world")
    expected = "hello world"
    assert result == expected, f"Expected '{expected}', got '{result}'"
    print(f"[PASS] Test 4: 'hello world' remains unchanged")


def test_longest_match_wins():
    """Test that longest match wins when one phrase is a prefix of another."""
    temp_dir = Path(tempfile.mkdtemp())

    # Create a dictionary with overlapping phrases
    dictionary = [
        {"phrase": "micro", "replacement": "Micro", "frequency": 5},
        {"phrase": "microphone", "replacement": "Microphone", "frequency": 10},
    ]

    dict_path = temp_dir / "dictionary.yaml"
    with open(dict_path, "w", encoding="utf-8") as f:
        yaml.dump(dictionary, f)

    snippets_path = temp_dir / "snippets.yaml"
    with open(snippets_path, "w", encoding="utf-8") as f:
        yaml.dump([], f)

    engine = VocabEngine(dict_path, snippets_path)

    # "microphone" should match the longer phrase
    result = engine.apply("I have a microphone")
    expected = "I have a Microphone"
    assert result == expected, f"Expected '{expected}', got '{result}'"
    print(f"[PASS] Test 5: 'I have a microphone' -> '{result}' (longest match wins)")

    # "micro" should match the shorter phrase only when standalone
    result = engine.apply("micro management is bad")
    expected = "Micro management is bad"
    assert result == expected, f"Expected '{expected}', got '{result}'"
    print(f"[PASS] Test 6: 'micro management is bad' -> '{result}'")


def test_vocab_for_prompt():
    """Test that vocab_for_prompt returns a comma-separated list."""
    _, dict_path, snippets_path = create_test_fixtures()
    engine = VocabEngine(dict_path, snippets_path)

    vocab_str = engine.vocab_for_prompt()
    print(f"[PASS] Test 7: vocab_for_prompt() = '{vocab_str}'")

    # Should contain all canonical phrases
    assert "Claude" in vocab_str
    assert "Alice" in vocab_str
    assert "Supabase" in vocab_str


def run_all_tests():
    """Run all tests."""
    print("Running vocabulary tests...\n")
    test_case_insensitive_replacement()
    print()
    test_word_not_in_list()
    print()
    test_longest_match_wins()
    print()
    test_vocab_for_prompt()
    print("\n[PASS] All tests passed!")


if __name__ == "__main__":
    run_all_tests()
