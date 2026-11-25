"""Headless / container mode: a stable access token, the secrets password check, health check,
and localnode looked up on an explicitly set mesh node."""
import pytest

from aredn_fleet import desktop, meshdns


def test_token_from_env_or_kept_in_the_config_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("AREDN_FLEET_HOME", str(tmp_path))
    monkeypatch.delenv("FLEET_TOKEN", raising=False)
    first = desktop.serve_token()
    assert len(first) >= 16 and desktop.serve_token() == first          # same after a restart
    assert (tmp_path / "config" / "serve-token").read_text() == first
    monkeypatch.setenv("FLEET_TOKEN", "x" * 20)
    assert desktop.serve_token() == "x" * 20
    monkeypatch.setenv("FLEET_TOKEN", "short")
    with pytest.raises(SystemExit):
        desktop.serve_token()


def test_encrypted_secrets_file(tmp_path, monkeypatch):
    pytest.importorskip("keyrings.cryptfile")
    import keyring
    f = tmp_path / "secrets" / "passwords.cfg"
    monkeypatch.setenv("FLEET_SECRETS_FILE", str(f))
    monkeypatch.delenv("FLEET_SECRETS_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="FLEET_SECRETS_PASSWORD"):
        desktop.configure_secrets()
    old = keyring.get_keyring()
    try:
        monkeypatch.setenv("FLEET_SECRETS_PASSWORD", "master-pw-1")
        desktop.configure_secrets()
        keyring.set_password("aredn-fleet", "clubpw", "NotARealPw-123")
        assert "NotARealPw-123" not in f.read_text()                   # encrypted on disk
        monkeypatch.setenv("FLEET_SECRETS_PASSWORD", "master-pw-1")      # restart: same file opens
        desktop.configure_secrets()
        assert keyring.get_password("aredn-fleet", "clubpw") == "NotARealPw-123"
        monkeypatch.setenv("FLEET_SECRETS_PASSWORD", "wrong")
        with pytest.raises(SystemExit, match="doesn't open"):
            desktop.configure_secrets()
    finally:
        keyring.set_keyring(old)


def test_localnode_uses_an_explicit_mesh_dns_server(monkeypatch):
    monkeypatch.delenv("AREDN_FLEET_DNS", raising=False)
    assert meshdns.mesh_name("localnode") is None
    monkeypatch.setenv("AREDN_FLEET_DNS", "10.99.154.177")
    assert meshdns.mesh_name("localnode") == "localnode.local.mesh"


def test_health_check_needs_no_token(tmp_path):
    from fastapi.testclient import TestClient
    from aredn_fleet.web.app import create_app
    from aredn_fleet.web.state import AppState
    c = TestClient(create_app(AppState(fw_dir=tmp_path / "fw", config_dir=tmp_path / "cfg"),
                              token="t" * 20))
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/api/info").status_code in (401, 403)
