#!/usr/bin/env python3
r"""
Vocabulary replacement engine.

Loads dictionary.yaml and snippets.yaml from the settings folder (see paths.py).
Implements longest-match-wins, case-insensitive replacement.

Rules:
1. For dictionary entries with a replacement, replace the spoken phrase.
2. For dictionary entries without a replacement, fix casing to canonical form.
3. For snippet triggers, replace with their text.
4. Deterministic: longest match wins. Words not in the list are untouched.
"""

import re
from pathlib import Path
from typing import Optional, List, Dict, Any
import yaml

from dictate import paths


class VocabEngine:
    """Longest-match-wins vocabulary replacement engine."""

    def __init__(
        self,
        dict_path: Optional[Path] = None,
        snippets_path: Optional[Path] = None,
    ):
        r"""
        Load vocabulary from YAML files.

        Args:
            dict_path: Path to dictionary.yaml. Defaults to %APPDATA%\dictate\dictionary.yaml
            snippets_path: Path to snippets.yaml. Defaults to %APPDATA%\dictate\snippets.yaml
        """
        if dict_path is None:
            dict_path = paths.settings_file("dictionary.yaml")
        if snippets_path is None:
            snippets_path = paths.settings_file("snippets.yaml")

        self.dict_path = dict_path
        self.snippets_path = snippets_path

        # Dictionary entries: {phrase_lower: (canonical_phrase, replacement_text|None)}
        self.vocab: Dict[str, tuple[str, Optional[str]]] = {}

        # Snippet triggers: {trigger_lower: replacement_text}
        self.snippets: Dict[str, str] = {}

        # All phrases sorted by length (longest first) for longest-match-wins
        self.phrases_by_length: List[str] = []

        self._load_files()

    def _load_files(self) -> None:
        """Load vocabulary and snippets from YAML files."""
        # Load dictionary
        if self.dict_path.exists():
            with open(self.dict_path, "r", encoding="utf-8") as f:
                entries = yaml.safe_load(f) or []
                for entry in entries:
                    phrase = entry.get("phrase", "")
                    replacement = entry.get("replacement")
                    phrase_lower = phrase.lower()
                    # Store both the canonical form and the replacement
                    self.vocab[phrase_lower] = (phrase, replacement)

        # Load snippets
        if self.snippets_path.exists():
            with open(self.snippets_path, "r", encoding="utf-8") as f:
                entries = yaml.safe_load(f) or []
                for entry in entries:
                    trigger = entry.get("trigger", "")
                    text = entry.get("text", "")
                    trigger_lower = trigger.lower()
                    self.snippets[trigger_lower] = text

        # Build phrase list sorted by length (longest first)
        all_phrases = list(self.vocab.keys()) + list(self.snippets.keys())
        self.phrases_by_length = sorted(
            set(all_phrases), key=lambda x: len(x), reverse=True
        )

    def apply(self, text: str) -> str:
        """
        Apply vocabulary and snippet replacements.

        Longest-match-wins, case-insensitive, whole-word matching.

        Args:
            text: The text to process.

        Returns:
            Processed text with replacements applied.
        """
        if not self.phrases_by_length:
            return text

        # Split into words and whitespace, preserving whitespace for reconstruction
        # Use a regex that splits on word boundaries but keeps the delimiters
        tokens = re.split(r"(\s+|[^\w])", text)

        result = []
        i = 0
        while i < len(tokens):
            token = tokens[i]

            # Skip whitespace and punctuation
            if not token or re.match(r"^\s+$", token) or re.match(
                r"^[^\w]+$", token
            ):
                result.append(token)
                i += 1
                continue

            # Try to match phrases starting at this position
            # Reconstruct the text from current position for phrase matching
            remaining_text = "".join(tokens[i:])
            matched = False

            for phrase in self.phrases_by_length:
                # Check if remaining_text starts with this phrase (case-insensitive)
                if remaining_text.lower().startswith(phrase):
                    # Make sure it's a whole-word match
                    match_end = len(phrase)
                    if (
                        match_end >= len(remaining_text)
                        or not remaining_text[match_end].isalnum()
                    ):
                        # We have a match
                        matched = True

                        # Check if it's a snippet or vocabulary
                        if phrase in self.snippets:
                            result.append(self.snippets[phrase])
                        else:
                            canonical_phrase, replacement = self.vocab[phrase]
                            if replacement:
                                # Replace with the replacement text
                                result.append(replacement)
                            else:
                                # Fix casing to canonical form
                                result.append(canonical_phrase)

                        # Skip the matched tokens
                        matched_len = 0
                        j = i
                        while j < len(tokens) and matched_len < len(phrase):
                            t = tokens[j]
                            if not re.match(r"^\s+$", t):
                                matched_len += len(t)
                            j += 1
                        i = j
                        break

            if not matched:
                result.append(token)
                i += 1

        return "".join(result)

    def vocab_for_prompt(self) -> str:
        """
        Return the vocabulary phrase list as a comma-separated string for the LLM prompt.

        Returns:
            Comma-separated list of canonical phrases.
        """
        canonical_phrases = sorted(
            set(phrase for phrase, _ in self.vocab.values()),
            key=str.lower,
        )
        return ", ".join(canonical_phrases)

    def write_hotwords_file(self, path: Path) -> None:
        """
        Write hotwords file (one phrase per line) for sherpa-onnx.

        Args:
            path: Path to write the hotwords file to.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        canonical_phrases = sorted(
            set(phrase for phrase, _ in self.vocab.values()),
            key=str.lower,
        )
        with open(path, "w", encoding="utf-8") as f:
            for phrase in canonical_phrases:
                f.write(phrase + "\n")


def get_vocab_engine(
    dict_path: Optional[Path] = None,
    snippets_path: Optional[Path] = None,
) -> VocabEngine:
    """Create and return a VocabEngine instance."""
    return VocabEngine(dict_path, snippets_path)


# Global engine instance for convenience
_engine: Optional[VocabEngine] = None


def init_engine(
    dict_path: Optional[Path] = None,
    snippets_path: Optional[Path] = None,
) -> None:
    """Initialize the global engine."""
    global _engine
    _engine = VocabEngine(dict_path, snippets_path)


def apply(text: str) -> str:
    """Apply vocabulary replacements using the global engine."""
    global _engine
    if _engine is None:
        init_engine()
    return _engine.apply(text)


def vocab_for_prompt() -> str:
    """Get vocabulary phrase list using the global engine."""
    global _engine
    if _engine is None:
        init_engine()
    return _engine.vocab_for_prompt()


def write_hotwords_file(path: Path) -> None:
    """Write hotwords file using the global engine."""
    global _engine
    if _engine is None:
        init_engine()
    _engine.write_hotwords_file(path)
