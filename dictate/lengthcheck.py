"""``python -m dictate.lengthcheck`` -- does dictate make more mistakes on
long clips than short ones?

Two sources, one report:

* ``test``: read-aloud takes against known text, five lengths from ~17 to
  ~200 words. Each take is scored three ways against the passage:
  what Parakeet heard, what Parakeet hears when the *same audio* is cut into
  ~12 s clips (isolates length from voice and mic), and what would actually
  have been pasted (cleanup under the live budget, raw text if it timed out).
  Takes are stored in ``%APPDATA%\\dictate\\lengthcheck.sqlite`` with audio,
  and re-scored every report, so the scoring can change without re-reading.
* ``report`` (the default): the test results, plus the live history bucketed
  by length -- how often cleanup landed or timed out, the wait, and how many
  words cleanup had to change (a proxy for mishearings; there is no reference
  text for real clips).

Reading is not speaking: the test measures the engine and cleanup, not the
restarts and fillers of a real monologue.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sqlite3
import statistics
import time
from difflib import SequenceMatcher
from pathlib import Path

DICTATE_DIR = Path(os.environ.get("APPDATA", "")) / "dictate"
DB_PATH = DICTATE_DIR / "lengthcheck.sqlite"
HISTORY_DB = DICTATE_DIR / "history.sqlite"

CHUNK_S = 12.0          # "short clip" length for the cut-up comparison
MIN_TAKES_PER_BAND = 2  # below this a band is reported but not ranked
CLEANUP_PATIENCE_S = 12.0  # test-only cleanup budget: long enough to see what cleanup needs

BANDS = [  # (label, min words inclusive, max words exclusive)
    ("under 25 words", 0, 25),
    ("25-60 words", 25, 60),
    ("60-110 words", 60, 110),
    ("110-170 words", 110, 170),
    ("170+ words", 170, 10**9),
]

# Two passages per length, written the way he dictates: work messages, British
# spelling, a few product names. Numbers spelled out so reference and output
# normalise to the same words.
PASSAGES = {
    "s1": "Can you move the call with the trustees to Thursday afternoon and send them the new link.",
    "s2": "The contact form on the donate page is broken again, so please check it before lunch.",
    "m1": ("I had a look at the homepage this morning and the main photo is cropped badly on mobile. "
           "The heading also wraps onto three lines, which pushes the donate button below the fold. "
           "Could we try a shorter heading and a smaller image?"),
    "m2": ("Quick update on the booking system. The calendar now shows which rooms are free each week, "
           "and the prices come straight from the spreadsheet. The only thing missing is the confirmation "
           "email, which I will finish tomorrow before the meeting."),
    "l1": ("Thanks for sending the analytics export. Most visitors arrive from Google and leave from the "
           "homepage within a few seconds, which tells me the first screen is not answering their question. "
           "The volunteering page gets far more visits than I expected, so I would move it into the main "
           "menu. The events page is almost empty, and I think that is because it still lists last "
           "year's dates. Can we fix the dates first and then look at the menu next week?"),
    "l2": ("I have moved the site from WordPress to a static build on Vercel, so pages now load in under "
           "a second. Nothing has changed for your team: you still edit the text in the same way, and the "
           "changes appear about a minute after you save. The old site stays online until you are happy "
           "with the new one, and switching over is a single change to the domain settings. I would "
           "suggest we do that on a quiet weekday morning."),
    "x1": ("Here is where things stand with the funding application. The first section, which describes "
           "the charity and its history, is finished and reads well. The second section asks for evidence "
           "of need, and at the moment we only have the survey from two years ago, which the funder may "
           "think is out of date. I would like to run a short survey of current families this month, just "
           "five or six questions, so the numbers are fresh. The budget section is mostly done, but the "
           "staff costs need checking against the new pay rates. The deadline is the end of next month, "
           "so there is time, but only if the survey goes out within a week. Could you let me know who on "
           "the team would be best placed to send it?"),
    "x2": ("I wanted to follow up on our conversation about the website. You mentioned that parents "
           "struggle to find the timetable, and when I tried it myself on my phone it took four taps and a "
           "download to see which sessions run on a Tuesday. My suggestion is to put the timetable directly "
           "on the page as plain text, with each day as a heading, so it can be read without opening a "
           "file. It would also mean search engines can find it, which should help new families who search "
           "for activities in the area. I can put together a rough version this week so you can see how it "
           "looks before anyone commits to anything. There is no cost for the draft, and if it is not "
           "useful we simply leave the current page as it is."),
    "y1": ("Notes from today's meeting with the board. Everyone agreed that the new website is a big "
           "improvement, and the chair asked for two changes before it goes live. The first is that the "
           "annual report should be linked from the footer rather than hidden in the about page. The second "
           "is that the donate button should take people straight to the payment form instead of a page of "
           "explanation. Both changes are small and I can make them tomorrow. There was also a longer "
           "discussion about security. The treasurer wanted to know who can log in to edit the site and "
           "what happens if a password is lost. I explained that each editor has their own account, that "
           "accounts can be removed in a minute, and that every change is saved so nothing can be lost for "
           "good. He asked for that in writing, so I will send a one page summary by Friday. Finally, the "
           "board wants to launch at the start of next month, to line up with the newsletter. That gives us "
           "three weeks, which is enough time, provided the photos from the summer event arrive this week. "
           "Could you chase the photographer for me?"),
    "y2": ("I have been thinking about how we price the monthly care plan, and I want to write it down "
           "before the call with the first client. The plan covers hosting, small text changes, and a "
           "check every month that the forms and links still work. What it does not cover is new pages or "
           "a redesign, which would be quoted separately. The risk with a flat monthly price is that one "
           "busy client uses far more time than the others, so I would put a soft limit of two hours of "
           "changes a month and tell them plainly when they are close to it. The other question is whether "
           "to offer a free first quarter. It lowers the barrier for a small charity, and it gives me a "
           "proper testimonial at the end, but it also means three months of work before any money comes "
           "in. I think the answer is yes for the first two clients only, and a normal price after that. "
           "I would also like every client to be able to leave at any time and take their site with them, "
           "because that is the fear I keep hearing in conversations with trustees."),
}


def band_of(words: int) -> str:
    for label, lo, hi in BANDS:
        if lo <= words < hi:
            return label
    return BANDS[-1][0]


# -- scoring -------------------------------------------------------------------

_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_CONTRACTIONS = {
    "i'm": "i am", "i've": "i have", "i'll": "i will", "i'd": "i would", "it's": "it is",
    "that's": "that is", "there's": "there is", "we're": "we are", "we'll": "we will",
    "you're": "you are", "don't": "do not", "doesn't": "does not", "isn't": "is not",
    "can't": "cannot", "won't": "will not", "let's": "let us", "they're": "they are",
}


def _num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " and " + _num_words(rest))
    return str(n)


def normalise(text: str) -> list:
    """Word list for scoring: case, punctuation, digits, contractions and
    -ize/-ise differences are not counted as mistakes."""
    t = (text or "").lower().replace("%", " percent ").replace("&", " and ")
    t = re.sub(r"[‘’]", "'", t)
    t = re.sub(r"\d+", lambda m: " " + _num_words(int(m.group())) + " ", t)
    t = re.sub(r"[^a-z' ]+", " ", t)
    out = []
    for w in t.split():
        w = w.strip("'")
        if not w:
            continue
        w = _CONTRACTIONS.get(w, w)
        w = re.sub(r"iz(e|es|ed|ing|ation|ations)$", r"is\1", w)
        out.extend(w.split())
    return out


def word_errors(ref: str, hyp: str) -> tuple:
    """(errors, reference words): word-level edit distance."""
    r, h = normalise(ref), normalise(hyp)
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1], len(r)


def split_points(audio, sr: int, chunk_s: float = CHUNK_S) -> list:
    """Cut points roughly every ``chunk_s`` seconds, each at the quietest
    50 ms frame within 3 s of the target, so words are not sliced in half."""
    import numpy as np
    frame = int(sr * 0.05)
    n_frames = len(audio) // frame
    if n_frames == 0:
        return []
    energy = np.sqrt(np.mean(np.square(audio[: n_frames * frame].reshape(n_frames, frame)), axis=1))
    cuts, t = [], chunk_s
    while t < len(audio) / sr - chunk_s / 3:
        lo, hi = int((t - 3) / 0.05), min(n_frames, int((t + 3) / 0.05))
        best = lo + int(np.argmin(energy[lo:hi]))
        cuts.append(best * frame + frame // 2)
        t = best * 0.05 + chunk_s
    return cuts


def transcribe_cut(audio, sr: int) -> str:
    from . import engine
    cuts = split_points(audio, sr)
    if not cuts:
        return engine.transcribe(audio)
    edges = [0] + cuts + [len(audio)]
    return " ".join(engine.transcribe(audio[a:b]) for a, b in zip(edges, edges[1:])).strip()


# -- storage -------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS Takes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL,
    passageId TEXT,
    duration REAL,
    asrText TEXT,
    cutText TEXT,
    pastedText TEXT,
    cleanup TEXT,
    asrMs REAL,
    cleanupMs REAL,
    audio BLOB
);
"""


#: Added 25 Sept for the HQ Dictation page: runs, the budget live would have
#: given, cleanup's full output even when live would have cut it off, mic peak.
_NEW_COLUMNS = [("runId", "TEXT"), ("liveBudgetS", "REAL"), ("cleanedText", "TEXT"), ("peak", "REAL")]


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(Takes)")}
    for name, typ in _NEW_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE Takes ADD COLUMN {name} {typ}")
    conn.commit()
    return conn


# -- the read-aloud test -------------------------------------------------------

def _pick_passages(conn: sqlite3.Connection) -> list:
    """One passage per length, the least-read of each pair, in random order."""
    counts = dict(conn.execute("SELECT passageId, COUNT(*) FROM Takes GROUP BY 1").fetchall())
    picks = []
    for prefix in "smlxy":
        ids = sorted(k for k in PASSAGES if k.startswith(prefix))
        picks.append(min(ids, key=lambda k: (counts.get(k, 0), random.random())))
    random.shuffle(picks)
    return picks


def live_call_cap() -> float:
    """The per-attempt limit live cleanup runs under: ``clean()``'s ``timeout``
    default, which the pipeline never overrides -- so the first model gets at
    most this long even when ``budget_s`` is larger (found 25 Sept)."""
    import inspect
    from . import cleanup as cleanup_mod
    return float(inspect.signature(cleanup_mod.clean).parameters["timeout"].default)


def process_take(conn: sqlite3.Connection, pid: str, audio, t_release: float,
                 no_cleanup: bool = False, run_id: str | None = None) -> tuple:
    """Run one take through the live chain (ASR, dictionary, cleanup), plus the
    cut-up ASR pass; store it. Returns (pasted, status).

    Cleanup is given ``CLEANUP_PATIENCE_S`` rather than the live budget, so the
    take records how long cleanup actually *needs*; ``pasted`` is then what live
    would have pasted -- the cleaned text if it finished inside the live budget,
    the raw text if not. That is what lets the page say "long clips need 3.1 s
    and get 2.0 s" instead of only "timed out"."""
    import numpy as np
    from . import cleanup as cleanup_mod, config, engine, history, pipeline
    from . import vocab as vocab_mod

    t0 = time.monotonic()
    asr = engine.transcribe(audio)
    asr_ms = (time.monotonic() - t0) * 1000
    text = vocab_mod.apply(asr) if asr else ""
    live_budget = min(pipeline.cleanup_budget(t_release), live_call_cap())  # after ASR, as live
    status, cleanup_ms, cleaned, pasted = "skipped:disabled", 0.0, text, text
    if text and not no_cleanup:
        language = cleanup_mod.resolve_language(config.language(), text)
        t0 = time.monotonic()
        cleaned, status = cleanup_mod.clean(text, "lengthcheck", vocab_mod.vocab_for_prompt(),
                                            timeout=CLEANUP_PATIENCE_S, budget=CLEANUP_PATIENCE_S,
                                            language=language)
        cleanup_ms = (time.monotonic() - t0) * 1000
        if status == "ok" and cleanup_ms / 1000 > live_budget:
            status = "skipped:timeout"  # what live would have logged
        pasted = cleaned if status == "ok" else text
    cut = transcribe_cut(audio, engine.SAMPLE_RATE)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    conn.execute(
        "INSERT INTO Takes (timestamp, passageId, duration, asrText, cutText, pastedText, "
        "cleanup, asrMs, cleanupMs, audio, runId, liveBudgetS, cleanedText, peak) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), pid, len(audio) / engine.SAMPLE_RATE, asr, cut, pasted, status,
         asr_ms, cleanup_ms, history._wav_bytes(audio, engine.SAMPLE_RATE),
         run_id, live_budget, cleaned, peak),
    )
    conn.commit()
    return pasted, status


def run_test(no_cleanup: bool = False) -> None:
    from . import capture, engine

    print("Loading the speech model...")
    engine.get_engine()
    rec = capture.Recorder()
    rec.open()
    conn = _connect()
    picks = _pick_passages(conn)
    print(f"\n{len(picks)} passages, shortest to longest in random order. For each: press Enter,\n"
          "read it aloud at your normal pace, press Enter when you finish.\n"
          "Type s + Enter to skip one, q + Enter to stop.\n")
    try:
        for n, pid in enumerate(picks, 1):
            ref = PASSAGES[pid]
            print("-" * 72)
            print(f"[{n}/{len(picks)}] {len(ref.split())} words\n\n{ref}\n")
            cmd = input("Enter to start > ").strip().lower()
            if cmd == "q":
                break
            if cmd == "s":
                continue
            rec.start()
            input("Recording... Enter to stop > ")
            audio = rec.stop()
            t_release = time.time()
            if len(audio) < engine.SAMPLE_RATE * 0.5:
                print("Too short, skipped.\n")
                continue

            pasted, status = process_take(conn, pid, audio, t_release, no_cleanup)
            e, w = word_errors(ref, pasted)
            print(f"\n{e} of {w} words wrong in what would have been pasted (cleanup: {status}).\n")
    finally:
        rec.close()
        conn.close()
    print()
    report()


# -- report --------------------------------------------------------------------

def _per100(errors: int, words: int) -> str:
    return "-" if not words else f"{100 * errors / words:.1f}"


def test_rows(db_path: Path = DB_PATH) -> list:
    """One dict per band that has takes: errors per 100 words, three ways."""
    if not db_path.exists():
        return []
    conn = _connect(db_path)
    takes = conn.execute("SELECT passageId, asrText, cutText, pastedText, cleanup, asrMs, cleanupMs "
                         "FROM Takes").fetchall()
    conn.close()
    acc: dict = {}
    for t in takes:
        ref = PASSAGES.get(t["passageId"])
        if not ref:
            continue
        a = acc.setdefault(band_of(len(ref.split())), {"takes": 0, "words": 0, "heard": 0, "cut": 0,
                                                       "pasted": 0, "landed": 0, "wait": []})
        a["takes"] += 1
        for key, col in (("heard", "asrText"), ("cut", "cutText"), ("pasted", "pastedText")):
            e, w = word_errors(ref, t[col])
            a[key] += e
        a["words"] += w
        a["landed"] += t["cleanup"] == "ok"
        a["wait"].append(((t["asrMs"] or 0) + (t["cleanupMs"] or 0)) / 1000)
    rows = []
    for label, _, _ in BANDS:
        if label in acc:
            a = acc[label]
            rows.append({"band": label, **{k: a[k] for k in ("takes", "words", "heard", "cut", "pasted", "landed")},
                         "wait": statistics.median(a["wait"])})
    return rows


def history_rows(db_path: Path = HISTORY_DB) -> list:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT asrText, formattedText, cleanup, e2eLatency FROM History "
                            "WHERE asrText IS NOT NULL AND asrText != ''").fetchall()
    finally:
        conn.close()
    acc: dict = {}
    for r in rows:
        raw = normalise(r["asrText"])
        a = acc.setdefault(band_of(len(raw)), {"clips": 0, "words": 0, "ok": 0, "timeout": 0,
                                               "changed": 0, "removed": 0, "wait": []})
        a["clips"] += 1
        a["ok"] += r["cleanup"] == "ok"
        a["timeout"] += r["cleanup"] == "skipped:timeout"
        if r["e2eLatency"] is not None:
            a["wait"].append(r["e2eLatency"])
        if r["cleanup"] == "ok":
            a["words"] += len(raw)
            for op, i1, i2, j1, j2 in SequenceMatcher(None, raw, normalise(r["formattedText"]),
                                                      autojunk=False).get_opcodes():
                if op == "replace":
                    a["changed"] += min(i2 - i1, j2 - j1)
                    a["removed"] += max(0, (i2 - i1) - (j2 - j1))
                elif op == "delete":
                    a["removed"] += i2 - i1
    return [{"band": label, **acc[label], "wait": statistics.median(acc[label]["wait"]) if acc[label]["wait"] else None}
            for label, _, _ in BANDS if label in acc]


def verdict(rows: list) -> str:
    ranked = [r for r in rows if r["takes"] >= MIN_TAKES_PER_BAND]
    if len(ranked) < 2:
        return ("Not enough takes yet to call it: run `lengthcheck test` until every length has "
                f"at least {MIN_TAKES_PER_BAND} takes (one run = one take per length).")
    best = min(ranked, key=lambda r: r["pasted"] / r["words"])
    worst = max(ranked, key=lambda r: r["pasted"] / r["words"])
    line = (f"Fewest mistakes at {best['band']}: {_per100(best['pasted'], best['words'])} wrong words in "
            f"every 100 pasted. Most at {worst['band']}: {_per100(worst['pasted'], worst['words'])}.")
    long_ = [r for r in ranked if r["cut"] < r["heard"] and r["band"] not in (BANDS[0][0], BANDS[1][0])]
    if long_:
        line += " Cutting the same audio into short clips heard fewer words wrong at " + \
                ", ".join(r["band"] for r in long_) + ", so the length itself is costing accuracy there."
    return line


def report(db_path: Path = DB_PATH) -> None:
    rows = test_rows(db_path)
    print("READ-ALOUD TEST  (wrong words per 100; lower is better)\n")
    if rows:
        print(f"  {'length':<16}{'takes':>6}{'heard':>8}{'cut up':>8}{'pasted':>8}{'cleanup ok':>12}{'wait s':>8}")
        for r in rows:
            print(f"  {r['band']:<16}{r['takes']:>6}{_per100(r['heard'], r['words']):>8}"
                  f"{_per100(r['cut'], r['words']):>8}{_per100(r['pasted'], r['words']):>8}"
                  f"{r['landed']:>6} of {r['takes']:<3}{r['wait']:>8.1f}")
        print("\n  heard  = what the speech model wrote for the whole take")
        print(f"  cut up = the same audio cut into ~{CHUNK_S:.0f} s clips, as if you had paused and released")
        print("  pasted = what dictate would have pasted (raw text wherever cleanup timed out)\n")
    print("  " + verdict(rows) + "\n")

    hist = history_rows()
    print("YOUR REAL CLIPS  (no reference text; cleanup's edits are the nearest thing to a mistake count)\n")
    if not hist:
        print("  No history yet.\n")
        return
    print(f"  {'length':<16}{'clips':>6}{'cleanup ok':>12}{'timed out':>11}{'fixed/100':>11}{'cut/100':>9}{'wait s':>8}")
    for r in hist:
        wait = f"{r['wait']:.1f}" if r["wait"] is not None else "-"
        print(f"  {r['band']:<16}{r['clips']:>6}{100 * r['ok'] / r['clips']:>11.0f}%{100 * r['timeout'] / r['clips']:>10.0f}%"
              f"{_per100(r['changed'], r['words']):>11}{_per100(r['removed'], r['words']):>9}{wait:>8}")
    print("\n  fixed/100 = words cleanup swapped for another word (mishearings, grammar)")
    print("  cut/100   = words cleanup removed (fillers, restarts)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m dictate.lengthcheck")
    sub = ap.add_subparsers(dest="cmd")
    t = sub.add_parser("test", help="read five passages aloud and score them")
    t.add_argument("--no-cleanup", action="store_true", help="score the speech model only (no paid call)")
    sub.add_parser("report", help="print the report (default)")
    args = ap.parse_args()
    if args.cmd == "test":
        run_test(no_cleanup=args.no_cleanup)
    else:
        report()


if __name__ == "__main__":
    main()
