#!/usr/bin/env python3
"""
launcher.py — Native desktop launcher for Scribe Library.

Starts the Flask server on a free port in a background thread, then opens
a native macOS window via pywebview pointing at it. When the user closes
the window, the Python process shuts down (Flask thread is daemonized).

This is the entry point for a packaged macOS .app bundle, but it also works
as a normal Python script for development:

    npm run build && python3 engine/launcher.py

Design notes:
  - Flask runs in a thread, not a subprocess, so we get a single .app bundle
    instead of needing to ship two binaries
  - Werkzeug's auto-reloader is disabled (it forks the process which breaks
    pywebview's main-thread requirement on macOS)
  - We bind to 127.0.0.1 only — never expose this to the network
  - The library directory moves to a per-user location when running as a
    bundled app, so writes don't try to land inside the read-only .app

Packaging this into a .app bundle is a future step — the launcher itself is
ready to be a PyInstaller entry point, but the spec file + native dep
bundling (Pango, Cairo, libusb) hasn't been written yet.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

# Configure logging early so errors during startup are visible
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("launcher")


def is_bundled() -> bool:
    """True if running inside a PyInstaller-packaged .app bundle."""
    return getattr(sys, "frozen", False)


def find_free_port() -> int:
    """
    Find an unused TCP port. Bind to port 0 (kernel picks one), grab the
    chosen port, then release it. There's a tiny race window between releasing
    and the server binding, but for localhost it's fine in practice.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_library_root() -> Path:
    """
    Where the library folder lives.

    - Bundled app: ~/Library/Application Support/Scribe Library/library/
    - Dev mode:   <repo>/library/

    Trying to write inside the .app bundle would either fail or pollute
    the user's app, so we always use a per-user data directory in bundle mode.
    """
    if is_bundled():
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "Scribe Library"
        else:
            base = Path(os.environ.get(
                "XDG_DATA_HOME", Path.home() / ".local" / "share"
            )) / "scribe-library"
        return base / "library"

    # Dev mode: <repo>/library (launcher.py lives in <repo>/engine)
    return Path(__file__).resolve().parent.parent / "library"


def get_resource_path(name: str) -> Path:
    """
    Resolve a path that's bundled inside the .app or sits next to launcher.py
    in dev mode. PyInstaller extracts data files to sys._MEIPASS at runtime.
    """
    if is_bundled() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent / name


def configure_server() -> "tuple[object, int]":
    """
    Import and configure the Flask app, then return (app, port).
    Done lazily (not at module import time) so launcher.py imports cheaply
    and any import errors surface in our managed error UI rather than at
    Python startup.
    """
    if is_bundled():
        sys.path.insert(0, str(get_resource_path(".")))

    import server as scribe_server  # noqa: E402

    library_root = get_library_root()
    library_root.mkdir(parents=True, exist_ok=True)
    log.info("Library root: %s", library_root)

    # Wire the server's globals — it normally derives these from CLI args
    scribe_server.LIBRARY_ROOT = library_root
    scribe_server.PATHS = scribe_server.init_library(library_root)
    scribe_server.REGISTRY = scribe_server.init_registry(scribe_server.PATHS)

    port = find_free_port()
    log.info("Will bind to port %d", port)
    return scribe_server.app, port


def run_server(app, port: int):
    """Run the Flask server. Called in a daemon thread."""
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", port, app, threaded=True)
    log.info("Server listening on http://127.0.0.1:%d", port)
    try:
        server.serve_forever()
    except Exception as e:
        log.error("Server crashed: %s", e, exc_info=True)


def wait_for_server(port: int, timeout: float = 10.0) -> bool:
    """
    Poll the port until something accepts connections, or timeout.
    Without this, opening the webview before the server is ready shows a
    "can't connect" page that doesn't refresh.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def show_error_window(title: str, message: str):
    """
    Display a fatal-error message in a webview window. We use this when the
    server fails to start — alternative would be a print to stderr that the
    user never sees.
    """
    import html as _html
    body = (
        f"<html><body style='font-family: -apple-system, sans-serif; "
        f"padding: 32px; max-width: 600px; line-height: 1.5;'>"
        f"<h1 style='color: #b04040'>Scribe Library couldn't start</h1>"
        f"<p><strong>{_html.escape(title)}</strong></p>"
        f"<pre style='background: #f4f4f4; padding: 12px; border-radius: 4px; "
        f"overflow-x: auto; font-size: 12px'>{_html.escape(message)}</pre>"
        f"<p style='color: #666; font-size: 13px'>"
        f"If this keeps happening, copy the message above and report it.</p>"
        f"</body></html>"
    )
    try:
        import webview
        webview.create_window("Scribe Library — Error", html=body,
                              width=700, height=500)
        webview.start()
    except Exception:
        print(f"\nERROR: {title}\n{message}", file=sys.stderr)


def main():
    log.info("Scribe Library launcher starting (bundled=%s)", is_bundled())

    # 1. Configure the Flask server
    try:
        app, port = configure_server()
    except Exception as e:
        log.exception("Failed to configure server")
        import traceback
        show_error_window(
            "Failed to start the server",
            f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}",
        )
        return 1

    # 2. Spin it up in a daemon thread so it dies with the main process
    server_thread = threading.Thread(
        target=run_server, args=(app, port),
        daemon=True, name="flask-server",
    )
    server_thread.start()

    # 3. Wait for it to actually be reachable
    if not wait_for_server(port):
        show_error_window(
            "Server didn't start in time",
            f"The Flask server failed to bind to port {port} within 10 seconds. "
            f"This usually means another error happened during startup — check "
            f"the console log.",
        )
        return 1

    # 4. Open the native window
    try:
        import webview
    except ImportError as e:
        log.error("pywebview not installed: %s", e)
        # Fall back to opening the user's default browser. This still works,
        # just doesn't look like a native app.
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
        log.info("Opened in default browser. Press Ctrl-C to stop.")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
        return 0

    log.info("Opening native window")
    webview.create_window(
        "Scribe Library",
        f"http://127.0.0.1:{port}",
        width=1280,
        height=900,
        min_size=(800, 600),
        resizable=True,
        confirm_close=False,
    )
    # Start the webview event loop. Returns when the user closes the window.
    webview.start(debug=False)

    log.info("Window closed, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
