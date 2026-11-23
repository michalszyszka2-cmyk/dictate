"""Evaluate dictate's cleanup prompt against Wispr Flow's own formatted text.

Runs ``dictate.cleanup.clean()`` over held-out asr -> formatted pairs with the
real foreground app name, and scores ours against Wispr's with a word-level
Levenshtein distance normalised by Wispr's word count.  Prints every row, then
median / p90 latency, median distance, the five worst rows, the model id used
and the number of skipped calls.  The full output is written to
``proof/eval_cleanup.txt``.

Sources of pairs, in order:

  DICTATE_EVAL_JSON   a JSON list of {asr, formatted, app} -- small hand sets
  DICTATE_EVAL_DB     a SQLite file with Wispr's History schema
                      (default: the live %APPDATA%\\Wispr Flow\\flow.sqlite,
                      always opened read-only)
  recovered snapshot  %APPDATA%\\dictate\\recovered-history.json, produced by
                      tests/recover_pairs.py after Wispr Flow 1.6.897 emptied
                      the live database on 21 Sep 2026

With a database or the recovered snapshot: rows where asrText != formattedText
and asrText has >= 15 words, most recent first.  The 40 most recent are the
STUDY set the prompt was written from and are excluded; the next 40 are the
EVAL set used here.  The two never overlap.

Usage:
  python tests/eval_cleanup.py [--quiet] [--limit N] [--model <openrouter id>]
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPDIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dictate")
RECOVERED = os.path.join(APPDIR, "recovered-history.json")
LIVE_DB = os.path.join(os.environ.get("APPDATA", ""), "Wispr Flow", "flow.sqlite")
OUT = os.path.join(REPO, "proof", "eval_cleanup.txt")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

FALLBACK_VOCAB = (
    "Claude, Supabase, Firecrawl, Vercel, Apify, Zapmail, Instantly, OpenRouter, Kimi, "
    "Antigravity, Parakeet, Wispr Flow, cal.com, Brave")

try:                                    # stream C's imported Wispr dictionary
    from dictate.vocab import vocab_for_prompt
    VOCAB = vocab_for_prompt() or FALLBACK_VOCAB
except Exception:
    VOCAB = FALLBACK_VOCAB


# ------------------------------------------------------------------ scoring
def wlev(a: str, b: str) -> int:
    """Word-level Levenshtein distance, case-insensitive, punctuation kept."""
    x, y = a.split(), b.split()
    if not x:
        return len(y)
    if not y:
        return len(x)
    x = [w.lower() for w in x]
    y = [w.lower() for w in y]
    prev = list(range(len(y) + 1))
    for i, xi in enumerate(x, 1):
        cur = [i] + [0] * len(y)
        for j, yj in enumerate(y, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (xi != yj))
        prev = cur
    return prev[-1]


def ndist(ours: str, wispr: str) -> float:
    n = max(1, len(wispr.split()))
    return wlev(ours, wispr) / n


# ------------------------------------------------------------------ pair loading
def _from_json_env(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = []
    for r in data:
        asr = r.get("asr") or r.get("asrText") or ""
        fmt = r.get("formatted") or r.get("formattedText") or ""
        if asr and fmt:
            rows.append({"asr": asr, "formatted": fmt, "app": r.get("app") or ""})
    return rows, "DICTATE_EVAL_JSON=%s (%d pairs, used whole)" % (path, len(rows))


def _split(rows):
    """rows oldest-first -> (eval_set, note).  Newest 40 = study, next 40 = eval."""
    usable = [r for r in rows
              if r["asr"] != r["formatted"] and len(r["asr"].split()) >= 15]
    study, ev = usable[-40:], usable[-80:-40]
    note = "%d usable pairs; STUDY = newest 40, EVAL = next 40 (no overlap)" % len(usable)
    if not ev:
        ev, note = study, note + " -- fewer than 80 pairs, falling back to the newest 40"
    return ev, note


def _from_db(path: str):
    if not os.path.exists(path):
        return [], "no database at %s" % path
    uri = "file:%s?mode=ro" % path.replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    try:
        cur = con.execute(
            "SELECT asrText, formattedText, COALESCE(app,''), COALESCE(timestamp,'') "
            "FROM History WHERE asrText IS NOT NULL AND formattedText IS NOT NULL "
            "ORDER BY timestamp")
        rows = [{"asr": a, "formatted": f, "app": ap, "ts": ts}
                for a, f, ap, ts in cur.fetchall()]
    finally:
        con.close()
    if not rows:
        return [], "%s has 0 History rows" % path
    ev, note = _split(rows)
    return ev, "DICTATE_EVAL_DB=%s -- %s" % (path, note)


def _from_recovered():
    if not os.path.exists(RECOVERED):
        return [], "no recovered snapshot at %s" % RECOVERED
    with open(RECOVERED, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    rows = [{"asr": r["asrText"], "formatted": r["formattedText"],
             "app": r.get("app", ""), "ts": r.get("timestamp", "")} for r in raw]
    ev, note = _split(rows)
    return ev, "recovered snapshot %s -- %s" % (RECOVERED, note)


def load_pairs():
    js = os.environ.get("DICTATE_EVAL_JSON")
    if js:
        return _from_json_env(js)
    db = os.environ.get("DICTATE_EVAL_DB", LIVE_DB)
    rows, note = _from_db(db)
    if rows:
        return rows, note
    rec, note2 = _from_recovered()
    return rec, note + " | " + note2


# ------------------------------------------------------------------ run
def main(argv):
    quiet = "--quiet" in argv
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    if "--model" in argv:
        os.environ["DICTATE_MODEL"] = argv[argv.index("--model") + 1]
    out_path = OUT
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]
        if not os.path.isabs(out_path):
            out_path = os.path.join(REPO, "proof", out_path)

    from dictate import cleanup as C

    buf = io.StringIO()

    def say(line=""):
        buf.write(line + "\n")
        if not quiet:
            print(line)

    pairs, source = load_pairs()
    if limit:
        pairs = pairs[:limit]

    ids = C.warmup()
    model = ids.get("openrouter") or "(none)"
    say("dictate cleanup eval -- %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    say("source : %s" % source)
    say("pairs  : %d" % len(pairs))
    say("model  : OpenRouter %s   (fallback: NVIDIA NIM %s)"
        % (model, ids.get("nvidia") or "(none)"))
    say("timeout: 1.2 s per call, chain budget %s s, up to %d free ids per clip"
        % (os.environ.get("DICTATE_CLEANUP_BUDGET", "1.2"), C.FREE_ATTEMPTS))
    say("paid   : %s" % ("ENABLED" if os.environ.get("DICTATE_PAID") == "1" else "off"))
    say("=" * 100)

    results = []
    for i, p in enumerate(pairs, 1):
        t0 = time.monotonic()
        out, status = C.clean(p["asr"], p["app"], VOCAB, timeout=1.2)
        dt = time.monotonic() - t0
        tried = list(C.LAST.get("tried") or [])
        answered = C.LAST.get("model") or ""
        call = next((t[1] for t in tried if t[2] == "ok"), None)
        d = ndist(out, p["formatted"])
        base = ndist(p["asr"], p["formatted"])
        results.append({"i": i, "app": p["app"], "asr": p["asr"],
                        "wispr": p["formatted"], "ours": out, "status": status,
                        "lat": dt, "nd": d, "base": base, "model": answered,
                        "call": call, "tried": tried})
        say("--- row %d  app=%s  status=%s  latency=%.2fs%s  ndist=%.3f "
            "(raw baseline %.3f)  via=%s"
            % (i, p["app"] or "?", status, dt,
               ("  answering call %.2fs" % call) if call else "",
               d, base, answered or "-"))
        say("tried : " + ", ".join("%s %.2fs %s" % t for t in tried))
        say("RAW   : " + p["asr"])
        say("WISPR : " + p["formatted"])
        say("OURS  : " + out)

    say("=" * 100)
    if not results:
        say("NO PAIRS -- eval pending: needs Wispr history or a week of dictate's own "
            "history.sqlite")
    else:
        lats = sorted(r["lat"] for r in results)
        nds = [r["nd"] for r in results]
        bases = [r["base"] for r in results]
        ok = [r for r in results if r["status"] == "ok"]
        skipped = [r for r in results if r["status"].startswith("skipped")]
        p90 = lats[max(0, int(round(0.9 * len(lats))) - 1)]
        say("rows              : %d" % len(results))
        say("model             : %s" % model)
        say("latency median    : %.2f s" % statistics.median(lats))
        say("latency p90       : %.2f s" % p90)
        say("latency max       : %.2f s" % lats[-1])
        say("ndist median      : %.3f   (raw-vs-Wispr baseline %.3f)"
            % (statistics.median(nds), statistics.median(bases)))
        if ok:
            say("ndist median (ok) : %.3f over %d answered rows"
                % (statistics.median([r["nd"] for r in ok]), len(ok)))
        say("skipped calls     : %d  %s"
            % (len(skipped),
               sorted({r["status"] for r in skipped}) if skipped else ""))
        calls = sorted(r["call"] for r in ok if r["call"])
        if calls:
            say("answering call    : median %.2f s, p90 %.2f s, max %.2f s "
                "(the winning request only)"
                % (statistics.median(calls),
                   calls[max(0, int(round(0.9 * len(calls))) - 1)], calls[-1]))
        by_model = {}
        for r in results:
            for mid, lat, st in r["tried"]:
                agg = by_model.setdefault(mid, {"ok": 0, "fail": 0, "lat": []})
                if st == "ok":
                    agg["ok"] += 1
                    agg["lat"].append(lat)
                else:
                    agg["fail"] += 1
        say("per model         :")
        for mid, agg in sorted(by_model.items(), key=lambda kv: -kv[1]["ok"]):
            med = statistics.median(agg["lat"]) if agg["lat"] else float("nan")
            say("    %-52s answered %2d, failed %2d, median %.2f s"
                % (mid, agg["ok"], agg["fail"], med))
        say("verbatim rows     : %d" % sum(1 for r in results if r["status"] == "verbatim"))
        say("p90 <= 1.2 s      : %s" % ("YES" if p90 <= 1.2 else "NO"))
        say()
        say("5 worst rows by normalised distance")
        say("-" * 100)
        for r in sorted(results, key=lambda r: -r["nd"])[:5]:
            say("row %d  app=%s  ndist=%.3f  (raw baseline %.3f)  status=%s"
                % (r["i"], r["app"] or "?", r["nd"], r["base"], r["status"]))
            say("  RAW   : " + r["asr"])
            say("  WISPR : " + r["wispr"])
            say("  OURS  : " + r["ours"])
            say()

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(buf.getvalue())
    print("written: %s (%d lines)" % (out_path, buf.getvalue().count("\n")))
    if quiet and results:
        lats = sorted(r["lat"] for r in results)
        print("model=%s rows=%d median=%.2fs p90=%.2fs ndist_median=%.3f skipped=%d"
              % (model, len(results), statistics.median(lats),
                 lats[max(0, int(round(0.9 * len(lats))) - 1)],
                 statistics.median([r["nd"] for r in results]),
                 sum(1 for r in results if r["status"].startswith("skipped"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
