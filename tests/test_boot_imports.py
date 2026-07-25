"""Starting the app must not pull in the heavy libraries.

On a host that sleeps, every second of startup counts: if the process takes too
long, Telegram drops the webhook and the bot stops answering. pandas,
matplotlib, reportlab, openpyxl and pdfplumber add several seconds and are only
needed when building a report or uploading a statement, not at boot.

The check runs in a clean subprocess because the rest of the suite already
imports those libraries, so inside the pytest process they would always be in
sys.modules.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEAVY = ["matplotlib", "reportlab", "openpyxl", "pandas", "pdfplumber", "yfinance"]

PROBE = f"""
import json, sys
sys.path.insert(0, {str(ROOT)!r})
import app.main  # noqa: F401
print(json.dumps([m for m in {HEAVY!r} if m in sys.modules]))
"""


def test_boot_does_not_import_heavy_libraries():
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True, text=True, cwd=ROOT, check=True,
    )
    import json
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    assert loaded == [], (
        f"Importing app.main loads {loaded}. Move those imports inside the "
        "function that uses them (see app/routers/reports.py)."
    )
