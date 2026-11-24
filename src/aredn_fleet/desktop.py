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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(host: str, port: int, token: str):
    import uvicorn

    from .web.app import create_app

    app = create_app(token=token)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
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
    logging.basicConfig(level=logging.WARNING)
    token = secrets.token_urlsafe(24)
    port = _free_port()
    server, thread = start_server("127.0.0.1", port, token)
    url = f"http://127.0.0.1:{port}/?token={token}"
    try:
        import webview
    except Exception:  # noqa: BLE001 - any import problem: use the browser instead
        webview = None

    if webview is not None:
        try:
            webview.create_window("AREDN Fleet Manager", url, width=1320, height=860,
                                  min_size=(1000, 640))
            webview.start()
            return 0
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    print(f"Opening {url} in your browser. Close this window or press Ctrl+C to quit.")
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
