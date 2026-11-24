"""Desktop launcher: runs the local server and shows it in a native window.

The window uses pywebview (Edge WebView2 on Windows, WebKitGTK on Linux). If
pywebview isn't available, it falls back to opening the default browser. The
server only listens on 127.0.0.1, and every API call needs a random per-run
token, so other programs and other machines can't drive it.
"""

from __future__ import annotations

import logging
import secrets
import socket
import sys
import threading
import time
import webbrowser


def log_file():
    from . import paths
    d = paths.config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "fleet-manager.log"


def ensure_std_streams() -> None:
    """A windowed (no-console) Windows build starts with sys.stdout/stderr = None.

    Anything that writes or inspects them then crashes (uvicorn's log setup
    calls sys.stdout.isatty()). Point both at a log file so output has somewhere
    to go and the user has something to send when reporting a problem.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        f = open(log_file(), "a", encoding="utf-8", buffering=1)
    except OSError:
        import os
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def show_error(title: str, text: str) -> None:
    """Best-effort message box (Windows), else stderr."""
    print(f"{title}: {text}", file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
        except Exception:  # noqa: BLE001
            pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(host: str, port: int, token: str):
    import uvicorn

    from .web.app import create_app

    app = create_app(token=token)
    # log_config=None: don't let uvicorn install its console (isatty-probing) logging.
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False,
                            log_config=None)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True, name="fleet-server")
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("the local server didn't start")
    return server, t


def main() -> int:
    ensure_std_streams()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return _run_desktop()
    except Exception as exc:  # noqa: BLE001 - last resort: tell the user, don't vanish
        logging.exception("startup failed")
        show_error("AREDN Fleet Manager",
                   f"The app couldn't start:\n\n{type(exc).__name__}: {exc}\n\n"
                   f"Details are in {log_file()}")
        return 1


def _run_desktop() -> int:
    token = secrets.token_urlsafe(24)
    port = _free_port()
    server, thread = start_server("127.0.0.1", port, token)
    url = f"http://127.0.0.1:{port}/?token={token}"
    try:
        import webview
    except Exception:  # noqa: BLE001 - any import problem: use the browser instead
        logging.warning("pywebview unavailable; opening the browser instead", exc_info=True)
        webview = None

    if webview is not None:
        try:
            # text_select: pywebview disables selecting text by default, which made logs
            # impossible to copy.
            webview.create_window("AREDN Fleet Manager", url, width=1320, height=860,
                                  min_size=(1000, 640), text_select=True)
            webview.start()
            server.should_exit = True
            thread.join(timeout=5)
            return 0
        except Exception:  # noqa: BLE001 - e.g. WebView2 runtime missing
            logging.exception("app window failed; falling back to the browser")
            show_error("AREDN Fleet Manager",
                       "The app window couldn't open (is the Microsoft Edge WebView2 runtime "
                       "installed?). Opening Fleet Manager in your web browser instead.\n\n"
                       "Close this browser tab and end the app from Task Manager to quit.")

    webbrowser.open(url)
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
    return 0


def serve(host: str = "127.0.0.1", port: int = 8787) -> int:
    """Headless mode for a Linux box or Pi on the mesh; open the printed URL from any browser."""
    token = secrets.token_urlsafe(24)
    server, thread = start_server(host, port, token)
    shown = host if host not in ("0.0.0.0", "::") else socket.gethostname()
    print(f"AREDN Fleet Manager running. Open:\n  http://{shown}:{port}/?token={token}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Note: listening beyond this machine. Anyone with the link can manage your nodes; "
              "traffic is plain HTTP like the rest of the mesh.", file=sys.stderr)
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
