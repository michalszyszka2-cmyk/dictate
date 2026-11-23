#!/usr/bin/env python3
r"""
Import dictionary and snippets from Wispr Flow database.

Reads %APPDATA%\Wispr Flow\flow.sqlite (read-only) and writes:
  - %APPDATA%\dictate\dictionary.yaml
  - %APPDATA%\dictate\snippets.yaml

Idempotent; prints row counts written.
"""

import sqlite3
import os
import sys
from pathlib import Path
from typing import List, Dict, Any
import yaml

# Paths
WISPR_DB = Path.home() / "AppData" / "Roaming" / "Wispr Flow" / "flow.sqlite"
DICTATE_DIR = Path.home() / "AppData" / "Roaming" / "dictate"
DICT_YAML = DICTATE_DIR / "dictionary.yaml"
SNIPPETS_YAML = DICTATE_DIR / "snippets.yaml"

# Junk rows to drop (vocabulary only)
JUNK_PHRASES = {
    "Code-\x900",  # Code-[binary char]90
}

# Snippet trigger to drop
JUNK_SNIPPET = "my Flow referral"


def read_wispr_db() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Read Dictionary table from Wispr Flow database (read-only).
    Returns (vocabulary_rows, snippet_rows).
    """
    if not WISPR_DB.exists():
        print(f"Warning: Wispr Flow database not found at {WISPR_DB}")
        return [], []

    try:
        # Open in read-only mode
        conn = sqlite3.connect(f"file:{WISPR_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        # Fallback: try without URI mode
        try:
            conn = sqlite3.connect(str(WISPR_DB), timeout=5.0)
        except Exception as e:
            print(f"Error opening database: {e}")
            return [], []

    cursor = conn.cursor()

    # Check if Dictionary table exists
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='Dictionary';"
    )
    if not cursor.fetchone():
        print("Warning: Dictionary table not found")
        conn.close()
        return [], []

    # Fetch all non-deleted rows
    cursor.execute(
        """
        SELECT phrase, replacement, isSnippet, frequencyUsed
        FROM Dictionary
        WHERE isDeleted = 0
        ORDER BY isSnippet, phrase
        """
    )
    rows = cursor.fetchall()
    conn.close()

    vocabulary = []
    snippets = []

    for phrase, replacement, is_snippet, frequency in rows:
        entry = {
            "phrase": phrase,
            "replacement": replacement,
            "frequency": frequency or 0,
        }

        if is_snippet == 0:
            # Vocabulary entry - drop junk
            if phrase not in JUNK_PHRASES:
                vocabulary.append(entry)
        else:
            # Snippet entry - drop "my Flow referral"
            if phrase != JUNK_SNIPPET:
                snippets.append(entry)

    return vocabulary, snippets


def write_yaml_files(vocabulary: List[Dict], snippets: List[Dict]) -> None:
    r"""Write YAML files to %APPDATA%\dictate."""
    # Create directory if needed
    DICTATE_DIR.mkdir(parents=True, exist_ok=True)

    # Write dictionary.yaml
    # Format: {phrase, replacement|null, frequency}
    dict_entries = []
    for entry in vocabulary:
        dict_entries.append(
            {
                "phrase": entry["phrase"],
                "replacement": entry["replacement"],
                "frequency": entry["frequency"],
            }
        )

    with open(DICT_YAML, "w", encoding="utf-8") as f:
        yaml.dump(dict_entries, f, allow_unicode=True, default_flow_style=False)

    # Write snippets.yaml
    # Format: {trigger, text}
    snippet_entries = []
    for entry in snippets:
        snippet_entries.append(
            {"trigger": entry["phrase"], "text": entry["replacement"]}
        )

    with open(SNIPPETS_YAML, "w", encoding="utf-8") as f:
        yaml.dump(snippet_entries, f, allow_unicode=True, default_flow_style=False)

    print(f"Wrote {len(dict_entries)} dictionary entries to {DICT_YAML}")
    print(f"Wrote {len(snippet_entries)} snippet entries to {SNIPPETS_YAML}")


def main():
    print("Importing from Wispr Flow...")
    vocabulary, snippets = read_wispr_db()
    write_yaml_files(vocabulary, snippets)


if __name__ == "__main__":
    main()
