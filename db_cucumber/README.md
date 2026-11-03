## Linux setup

Use a Linux interpreter and a fresh environment; do not reuse a macOS `.venv`:

```bash
python3 -m venv .venv-linux
source .venv-linux/bin/activate
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

Run the preserved single search or a bounded monitor scan:

```bash
HEADLESS=1 python real_search_probe.py
python db_monitor.py --start 2026-11-03 --end 2026-11-05 --report-dir artifacts
```
