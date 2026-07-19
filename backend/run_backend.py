"""PyInstaller entry point for the packaged backend.

Runs uvicorn against the imported app object (not an import string) so it works
inside a frozen single-file exe with no reloader/multiprocessing surprises.

Also watches the parent app: the Tauri shell passes its PID via FFXIV_PARENT_PID,
and if that process disappears (normal close, crash, or force-kill) we exit. This
is more robust than relying on the parent to kill us — PyInstaller onefile runs as
a bootloader+worker pair, so an external kill of the bootloader can orphan the
worker; the worker watching the parent itself closes that gap.
"""
import multiprocessing
import os
import threading
import time


def _watch_parent():
    pid = os.environ.get("FFXIV_PARENT_PID")
    if not pid:
        return
    pid = int(pid)
    import psutil

    while True:
        if not psutil.pid_exists(pid):
            os._exit(0)
        time.sleep(2)


def main():
    import uvicorn
    from app import app

    if os.environ.get("FFXIV_PARENT_PID"):
        threading.Thread(target=_watch_parent, daemon=True).start()

    port = int(os.environ.get("FFXIV_BACKEND_PORT", "8756"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for frozen exes
    main()
