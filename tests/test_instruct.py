"""Spoken-instruction detection and the no-LLM fallback strip."""
from dictate import instruct

T = ["dictate"]


def test_detects_trigger_at_start_and_after_full_stop():
    assert instruct.detect("Dictate, make this bullet points. Milk, eggs.", T)
    assert instruct.detect("Milk, eggs. Dictate: numbered list.", T)
    assert instruct.detect("Milk. Hey dictate, make it formal.", T)


def test_trigger_anywhere_as_a_word():
    assert instruct.detect("Jeszcze raz, dictate. Milk.", T)
    assert not instruct.detect("I want to dictation letter.", T)


def test_strip_removes_addressed_instruction_only():
    assert instruct.strip("Dictate, make this bullet points. Milk, eggs.", T) == "Milk, eggs."
    assert instruct.strip("Milk, eggs. Dictate: numbered list.", T) == "Milk, eggs."
    # No comma after the trigger: may be content, left alone.
    assert instruct.strip("Dictate is slow today.", T) == "Dictate is slow today."
