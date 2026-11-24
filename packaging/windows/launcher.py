"""Entry point for the packaged desktop app (PyInstaller).

``--selftest`` starts the bundled server, checks that the UI and the API
answer, and exits 0/1 without opening a window. CI runs it on every build so
a broken bundle (missing module or static file) fails the release.
"""

import sys


def selftest() -> int:
    import json
    import secrets
    import urllib.request

    from aredn_fleet.desktop import _free_port, start_server

    token = secrets.token_urlsafe(16)
    port = _free_port()
    server, thread = start_server("127.0.0.1", port, token)
    try:
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(base + "/", timeout=10).read().decode()
        js = urllib.request.urlopen(base + "/static/app.js", timeout=10).read()
        req = urllib.request.Request(base + "/api/info", headers={"x-fleet-token": token})
        info = json.load(urllib.request.urlopen(req, timeout=10))
        import asyncssh  # noqa: F401  (needed for node access)
        import keyring  # noqa: F401
        import webview  # noqa: F401
        ok = "Fleet Manager" in page and len(js) > 1000 and info.get("version")
        print("selftest", "ok" if ok else "FAILED", info)
        return 0 if ok else 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    from aredn_fleet.desktop import main

    sys.exit(main())
