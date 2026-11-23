r"""Keep tests out of the real %APPDATA%\dictate\history.sqlite.

pipeline.run() always calls history.log(), so every unit test that runs the
pipeline used to add a row to the user's real history (88 empty notepad.exe
rows with e2eLatency ~1.8e9 from t_release=0). Each test now gets its own
temp DB via DICTATE_HISTORY_DB. The Notepad e2e test is the exception: it
exists to put a real clip's latency into the real history.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_history_db(request, tmp_path, monkeypatch):
    if "e2e" in request.node.nodeid:
        return
    monkeypatch.setenv("DICTATE_HISTORY_DB", str(tmp_path / "history.sqlite"))
