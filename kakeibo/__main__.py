"""Entry point for `python -m kakeibo` or the PyInstaller exe."""
import os
import webbrowser
import threading


def _open_browser(port: int):
    webbrowser.open(f"http://localhost:{port}")


def main():
    # Prepare secrets before importing the app.
    # (Order matters because the app / ocr.gemini modules read env at import time.)
    from .config import prompt_missing_keys
    prompt_missing_keys()

    from . import config
    from .app import app, purge_orphan_images
    from .db import init_db

    init_db()

    # Housekeeping (ASCII console output only - see CLAUDE.md, cp949 consoles)
    dst = config.backup_db()
    if dst:
        print(f"[backup] {os.path.basename(dst)}")
    n = config.cleanup_temp()
    if n:
        print(f"[cleanup] removed {n} stale temp session(s)")
    n = purge_orphan_images()
    if n:
        print(f"[cleanup] removed {n} unreferenced receipt image(s)")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    # Automatically open the browser when the exe is double-clicked
    if os.environ.get("KAKEIBO_NO_BROWSER", "") != "1":
        threading.Timer(1.0, _open_browser, args=[port]).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
