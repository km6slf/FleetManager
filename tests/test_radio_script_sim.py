"""Run the generated on-node radio script under a POSIX shell with stubbed AREDN tools.

Paths are rewritten into a temp dir; ucode/node-setup/restart-services/reboot/
logger are stubs that log their calls. Covers: unconfirmed revert, confirm,
and the reboot path where the timer must restart from rc.local.
"""

import os
import shutil
import subprocess
import time

import pytest

from aredn_fleet.jobs.radio import build_apply_script

pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                                reason="needs a POSIX sh (the script runs on AREDN nodes, not Windows)")


def setup_sim(root, reboot_needed):
    (root / "bin").mkdir()
    (root / "etc/config.mesh").mkdir(parents=True)
    (root / "tmp").mkdir()
    (root / "etc/rc.local").write_text("# rc.local\n\nexit 0\n")
    (root / "etc/config.mesh/setup").write_text("option radio0_channel '131'\n")
    for c in ["node-setup", "restart-services", "reboot", "logger"]:
        f = root / "bin" / c
        f.write_text(f'#!/bin/sh\necho "{c}" >> {root}/log\n')
        f.chmod(0o755)
    flag = (f"mkdir -p {root}/tmp/reboot-required && touch {root}/tmp/reboot-required/reboot"
            if reboot_needed else "true")
    u = root / "bin/ucode"
    u.write_text(f"#!/bin/sh\necho ucode >> {root}/log\n"
                 f"echo \"option radio0_channel '133'\" > {root}/etc/config.mesh/setup\n{flag}\n")
    u.chmod(0o755)
    s = build_apply_script([(0, "channel", "133"), (0, "bandwidth", "10")], 0, 1)
    s = (s.replace("/etc/", f"{root}/etc/")
          .replace("/tmp/reboot-required", f"{root}/tmp/reboot-required")
          .replace("/usr/local/bin/", ""))
    (root / "apply.sh").write_text(s)
    env = dict(os.environ, PATH=f"{root}/bin:{os.environ['PATH']}")
    return env


def run(root, env, extra=None):
    subprocess.run(["sh", str(root / "apply.sh")], env=env, check=True, timeout=20)
    if extra:
        extra()
    time.sleep(2.5)
    return (root / "log").read_text(), (root / "etc/config.mesh/setup").read_text()


def assert_clean(root):
    assert "aredn-fleet" not in (root / "etc/rc.local").read_text()
    assert not (root / "etc/aredn-fleet-radio").exists()


def test_unconfirmed_change_reverts(tmp_path):
    env = setup_sim(tmp_path, reboot_needed=False)
    log, setup = run(tmp_path, env)
    assert "ucode" in log and "node-setup" in log
    assert "'131'" in setup
    assert_clean(tmp_path)


def test_confirmed_change_stays(tmp_path):
    env = setup_sim(tmp_path, reboot_needed=False)
    log, setup = run(tmp_path, env,
                     lambda: (tmp_path / "etc/aredn-fleet-radio/confirmed").touch())
    assert "'133'" in setup and "node-setup" not in log
    assert_clean(tmp_path)


def test_reboot_path_reverts_from_rc_local(tmp_path):
    env = setup_sim(tmp_path, reboot_needed=True)

    def boot():
        rc = (tmp_path / "etc/rc.local").read_text()
        lines = rc.splitlines()
        hook = next(l for l in lines if "aredn-fleet-radio" in l)
        assert lines.index(hook) < lines.index("exit 0")   # inserted before exit 0
        shutil.rmtree(tmp_path / "tmp/reboot-required")    # /tmp is wiped by the reboot
        subprocess.Popen(["sh", "-c", hook.rstrip(" &")], env=env)

    log, setup = run(tmp_path, env, boot)
    assert log.splitlines()[:2] == ["ucode", "reboot"]
    assert "'131'" in setup
    assert_clean(tmp_path)
