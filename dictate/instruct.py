"""Spoken instructions to the editor: "Dictate, make this bullet points."

A clip is in instruction mode when a trigger word (config.yaml
``instructions.trigger``, default ``dictate``) appears anywhere in it. The whole transcript then goes
to the LLM with ``prompts/instruct.txt``, which follows the instruction and
drops it from the output; the model is also told to ignore a trigger that is
only content ("Dictate is slow today").

Detection is deterministic so a normal clip never pays for instruction mode.
``strip`` is the fallback when the LLM call fails: it removes the instruction
sentence only when the trigger was followed by a comma or colon (a clear
address), so the paste never carries "Dictate, make this bullet points".
"""
from __future__ import annotations

import re


def _pattern(triggers) -> re.Pattern | None:
    words = [re.escape(t.strip()) for t in (triggers or []) if t and t.strip()]
    if not words:
        return None
    # Anywhere as a whole word ("Jeszcze raz, dictate." was missed when this
    # required a sentence start); the LLM decides whether it is addressed.
    return re.compile(r"(?:\b(?:hey|okay|ok)\s+)?\b(?:%s)\b(?P<addr>\s*[,:.])?"
                      % "|".join(words), re.IGNORECASE)


def detect(text: str, triggers) -> bool:
    """True when a trigger word appears in ``text``."""
    pat = _pattern(triggers)
    return bool(pat and text and pat.search(text))


def strip(text: str, triggers) -> str:
    """``text`` without its addressed instruction sentences ("Dictate, ... ."),
    for pasting when the LLM could not run. A trigger with no comma/colon
    after it is left alone -- it may be content, not an instruction."""
    pat = _pattern(triggers)
    if not pat or not text:
        return text
    out, pos = [], 0
    for m in pat.finditer(text):
        if not m.group("addr"):
            continue
        if "." in m.group("addr"):
            stop = m.end()                  # "..., dictate." -- the word alone
        else:
            end = re.search(r"[.!?](?:\s|$)", text[m.end():])
            stop = m.end() + end.end() if end else len(text)
        out.append(text[pos:m.start()])
        pos = stop
    out.append(text[pos:])
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()
