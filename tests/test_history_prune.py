"""Old clips lose their audio, keep their text; new clips keep both."""
import time

from dictate import history


def test_prune_audio_keeps_text(tmp_path):
    db = tmp_path / "h.sqlite"
    now = time.time()
    old = history.log("old", "Old.", "x.exe", "w", 1, 0.5, audio=[0.1] * 1600,
                      timestamp=now - 10 * 86400, db_path=db)
    new = history.log("new", "New.", "x.exe", "w", 1, 0.5, audio=[0.1] * 1600,
                      timestamp=now, db_path=db)
    assert history.prune_audio(7, db_path=db, now=now) == 1
    conn = history._connect(db)
    rows = dict(conn.execute("SELECT id, audio IS NOT NULL FROM History").fetchall())
    texts = [r[0] for r in conn.execute("SELECT formattedText FROM History ORDER BY id")]
    conn.close()
    assert rows == {old: 0, new: 1}
    assert texts == ["Old.", "New."]


def test_keep_audio_false_saves_no_audio(tmp_path):
    db = tmp_path / "h.sqlite"
    rid = history.log("a", "A.", "x.exe", "w", 1, 0.5, audio=[0.1] * 1600,
                      keep_audio=False, db_path=db)
    conn = history._connect(db)
    assert conn.execute("SELECT audio FROM History WHERE id=?", (rid,)).fetchone()[0] is None
    conn.close()
