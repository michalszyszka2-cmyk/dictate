"""JSON side of the length check, for the HQ Dictation page
(``hq/app/src/app/api/dictation``). Three calls, each prints one JSON line:

    python -m dictate.lengthcheck_api next
    python -m dictate.lengthcheck_api score --passage m1 --audio take.webm --run <id>
    python -m dictate.lengthcheck_api summary

``summary`` turns the takes into what the page says: one verdict sentence, the
changes worth making (each tied to the numbers that triggered it), results by
length, past runs, and the real-clip history.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import subprocess
import time
from pathlib import Path

from .lengthcheck import (BANDS, CHUNK_S, DB_PATH, HISTORY_DB, MIN_TAKES_PER_BAND, PASSAGES, _connect,
                          _pick_passages, band_of, history_rows, live_call_cap, normalise, process_take,
                          word_errors)


def _load_audio(path: str):
    """Any audio file the browser sends (webm/opus, wav, mp3) -> float32 16 kHz mono."""
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def next_set(db_path: Path = DB_PATH) -> dict:
    conn = _connect(db_path)
    try:
        picks = _pick_passages(conn)
    finally:
        conn.close()
    return {"ok": True, "passages": [{"id": p, "text": PASSAGES[p], "words": len(PASSAGES[p].split())}
                                     for p in picks]}


def score_file(pid: str, audio_path: str, run_id: str | None, db_path: Path = DB_PATH) -> dict:
    from . import engine
    if pid not in PASSAGES:
        return {"ok": False, "error": f"unknown passage {pid}"}
    audio = _load_audio(audio_path)
    if len(audio) < engine.SAMPLE_RATE * 0.5:
        return {"ok": False, "error": "the recording was under half a second"}
    import numpy as np
    engine.transcribe(np.zeros(engine.SAMPLE_RATE // 2, dtype=np.float32))  # live has the model loaded and warm
    t_release = time.time()
    conn = _connect(db_path)
    try:
        pasted, status = process_take(conn, pid, audio, t_release, run_id=run_id)
        row = conn.execute("SELECT cleanupMs, liveBudgetS, peak FROM Takes ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    e, w = word_errors(PASSAGES[pid], pasted)
    return {"ok": True, "passage": pid, "words": w, "wrong": e, "pasted": pasted, "cleanup": status,
            "cleanupS": round((row["cleanupMs"] or 0) / 1000, 2), "limitS": round(row["liveBudgetS"] or 0, 2),
            "seconds": round(len(audio) / engine.SAMPLE_RATE, 1), "peak": round(row["peak"] or 0, 3)}


def _missing_sentence(ref: str, out: str) -> str | None:
    """First reference sentence that cleanup dropped almost entirely."""
    have = set(normalise(out))
    for sent in re.split(r"(?<=[.?!])\s+", ref):
        words = normalise(sent)
        if len(words) >= 4 and sum(w in have for w in words) / len(words) < 0.4:
            return sent.strip()
    return None


def _band_rows(takes: list) -> list:
    acc: dict = {}
    for t in takes:
        ref = PASSAGES[t["passageId"]]
        b = acc.setdefault(band_of(len(ref.split())), {"takes": 0, "words": 0, "heard": 0, "cut": 0,
                                                       "pasted": 0, "cleaned": 0, "landed": 0,
                                                       "need": [], "limit": [], "asr": [], "dropped": None})
        heard, words = word_errors(ref, t["asrText"])
        b["takes"] += 1
        b["words"] += words
        b["heard"] += heard
        b["cut"] += word_errors(ref, t["cutText"])[0]
        b["pasted"] += word_errors(ref, t["pastedText"])[0]
        b["cleaned"] += word_errors(ref, t["cleanedText"] or t["pastedText"])[0]
        b["landed"] += t["cleanup"] == "ok"
        if t["cleanupMs"] and t["liveBudgetS"] is not None:  # older takes timed out, so never saw the full need
            b["need"].append(t["cleanupMs"] / 1000)
        if t["liveBudgetS"]:
            b["limit"].append(t["liveBudgetS"])
        if t["asrMs"]:
            b["asr"].append(t["asrMs"] / 1000)
        if t["cleanedText"] and not b["dropped"]:
            b["dropped"] = _missing_sentence(ref, t["cleanedText"])
    rows = []
    for label, _, _ in BANDS:
        if label not in acc:
            continue
        b = acc[label]
        per = {k: (round(100 * b[k] / b["words"], 1) if b["words"] else None)
               for k in ("heard", "cut", "pasted", "cleaned")}
        rows.append({"band": label, "takes": b["takes"], **per, "landed": b["landed"],
                     "needS": round(max(b["need"]), 2) if b["need"] else None,
                     "limitS": round(statistics.median(b["limit"]), 2) if b["limit"] else None,
                     "asrS": round(max(b["asr"]), 2) if b["asr"] else None,
                     "dropped": b["dropped"], "ranked": b["takes"] >= MIN_TAKES_PER_BAND})
    return rows


def _run_rows(takes: list) -> list:
    runs: dict = {}
    for t in takes:
        rid = t["runId"] or f"terminal-{int(t['timestamp'] // 3600)}"
        r = runs.setdefault(rid, {"id": rid, "at": t["timestamp"], "takes": 0, "words": 0, "wrong": 0})
        e, w = word_errors(PASSAGES[t["passageId"]], t["pastedText"])
        r["takes"] += 1
        r["wrong"] += e
        r["words"] += w
    return [{"id": r["id"], "at": r["at"], "takes": r["takes"],
             "pasted": round(100 * r["wrong"] / r["words"], 1) if r["words"] else None}
            for r in sorted(runs.values(), key=lambda r: r["at"], reverse=True)]


def _upper_words(label: str) -> int:
    for lab, lo, hi in BANDS:
        if lab == label:
            return lo if hi > 10**8 else hi
    return 0


def verdict_text(rows: list) -> str:
    ranked = [r for r in rows if r["ranked"]]
    if len(ranked) < 2:
        return ("Not enough readings yet. Run the test twice so every length has "
                f"{MIN_TAKES_PER_BAND} readings, and this line will tell you how long to talk.")
    best = min(r["pasted"] for r in ranked)
    fine = [r for r in ranked if r["pasted"] <= best + 2]
    longest_fine = fine[-1]
    worse = ranked[ranked.index(longest_fine) + 1:]
    if not worse:
        return "Length makes no difference: even the longest passages came out as clean as the short ones."
    w = worse[0]
    if w["landed"] < w["takes"]:
        why = "cleanup runs out of time and you get your words raw, fillers and all"
    elif (w["cleaned"] or 0) > (w["heard"] or 0) + 2:
        why = "cleanup starts rewriting and dropping what you said"
    else:
        why = "the speech model starts mishearing you"
    return (f"Talk in stretches of up to about {_upper_words(longest_fine['band'])} words, then release and "
            f"carry on. Past that, {why}.")


PASTE_RESERVE = 0.35  # pipeline.PASTE_RESERVE_S, kept out of the import so summary stays light


def _e2e_target() -> float:
    try:
        from . import config
        return float((config.cleanup_config() or {}).get("e2e_target_s", 1.4) or 1.4)
    except Exception:
        return 1.4


def advice(rows: list, peaks: list) -> list:
    out = []
    cap = live_call_cap()
    short = [r for r in rows if r["needS"] is not None and r["limitS"] is not None
             and r["needS"] > r["limitS"] and r["landed"] < r["takes"]]
    if short:
        need = max(r["needS"] for r in short)
        limit = min(r["limitS"] for r in short)
        asr = max((r["asrS"] or 0) for r in short)
        if limit < cap - 0.05:
            target = _e2e_target()
            new_target = math.ceil((asr + need + PASTE_RESERVE + 0.3) * 2) / 2
            out.append({
                "title": "Give cleanup more time on long clips",
                "body": (f"From {short[0]['band']} up, the speech model takes up to {asr:.1f} s to write the clip out, "
                         f"and dictate counts that against its {target:.1f} s target for the whole paste. That leaves "
                         f"cleanup only {limit:.2f} s, when it needs about {need:.1f} s, so long clips paste raw, "
                         f"fillers and all. Raising the target means a long clip arrives about "
                         f"{need - limit:.1f} s later, but cleaned. Short clips are not slowed down."),
                "change": rf"%APPDATA%\dictate\config.yaml: cleanup.e2e_target_s from {target:.1f} to {new_target:.1f}",
            })
        else:
            out.append({
                "title": "Give cleanup more time on long clips",
                "body": (f"From {short[0]['band']} up, cleanup needs up to {need:.1f} s but each attempt is cut off "
                         f"after {cap:.1f} s, a fixed limit in cleanup.py that wins over budget_s in config.yaml. "
                         "Raising it means long clips arrive a moment later, but cleaned."),
                "change": f"dictate/cleanup.py clean(timeout=) from {cap:.1f} to about {need + 0.5:.1f} s",
            })
    reword = [r for r in rows if r["cleaned"] is not None and r["heard"] is not None
              and r["cleaned"] > r["heard"] + 3]
    if reword:
        r = max(reword, key=lambda r: r["cleaned"] - r["heard"])
        body = (f"The speech model got your words right, but cleanup changed about "
                f"{r['cleaned'] - r['heard']:.0f} in every 100 on {r['band']} clips.")
        dropped = next((x["dropped"] for x in rows if x["dropped"]), None)
        if dropped:
            body += f' In one reading it deleted "{dropped}" entirely.'
        body += " The cleanup prompt is told to remove all fluff, and on work messages it cuts real content too."
        out.append({"title": "Cleanup is rewriting what you said", "body": body,
                    "change": "dictate/prompts/cleanup.txt: narrow \"remove all fluff\" to fillers and restarts"})
    worse_long = [r for r in rows if r["heard"] is not None and r["cut"] is not None
                  and r["heard"] > r["cut"] + 2 and r["band"] not in (BANDS[0][0], BANDS[1][0])]
    if worse_long:
        out.append({"title": "Release more often on long thoughts",
                    "body": (f"On {worse_long[0]['band']} the speech model misheard more than it did on the same "
                             f"audio cut into {CHUNK_S:.0f}-second pieces. Releasing every sentence or two keeps "
                             "it accurate."),
                    "change": None})
    if peaks and statistics.median(peaks) < 0.3 and any((r["heard"] or 0) > 4 for r in rows):
        out.append({"title": "Your mic is quiet",
                    "body": ("Your voice peaks at under a third of the mic's range, and quiet audio is where the "
                             "speech model mishears most. Sit closer or raise the input level in Windows sound "
                             "settings."),
                    "change": None})
    if not out and any(r["ranked"] for r in rows):
        out.append({"title": "Nothing to adjust",
                    "body": "Every length came out about as clean as the shortest one.", "change": None})
    return out


def summary(db_path: Path = DB_PATH, history_db: Path = HISTORY_DB) -> dict:
    takes = []
    if db_path.exists():
        conn = _connect(db_path)
        takes = [dict(r) for r in conn.execute(
            "SELECT timestamp, runId, passageId, asrText, cutText, pastedText, cleanedText, cleanup, "
            "asrMs, cleanupMs, liveBudgetS, peak FROM Takes ORDER BY timestamp")]
        conn.close()
    takes = [t for t in takes if t["passageId"] in PASSAGES]
    rows = _band_rows(takes)
    peaks = [t["peak"] for t in takes if t.get("peak")]
    return {"ok": True, "verdict": verdict_text(rows), "advice": advice(rows, peaks), "bands": rows,
            "runs": _run_rows(takes), "history": history_rows(history_db), "takes": len(takes),
            "minTakes": MIN_TAKES_PER_BAND, "callCapS": live_call_cap()}


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m dictate.lengthcheck_api")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next")
    sc = sub.add_parser("score")
    sc.add_argument("--passage", required=True)
    sc.add_argument("--audio", required=True)
    sc.add_argument("--run")
    sm = sub.add_parser("summary")
    sm.add_argument("--db", help="takes database (default: the real one in %%APPDATA%%)")
    args = ap.parse_args()
    if args.cmd == "next":
        data = next_set()
    elif args.cmd == "score":
        data = score_file(args.passage, args.audio, args.run)
    else:
        data = summary(Path(args.db)) if args.db else summary()
    print(json.dumps(data))


if __name__ == "__main__":
    main()
