from pathlib import Path

from aredn_fleet.install import METHODS, method_for, pick_images, stage_files


def test_methods_follow_the_catalog_ids():
    assert method_for("mikrotik_routerboard-952ui-5ac2nd").key == "mikrotik-pxe"
    assert method_for("mikrotik_hap-ac2").key == "mikrotik-pxe"
    assert method_for("tplink_cpe210-v3").key == "tplink"
    assert method_for("ubnt_nanostation-m-xw").key == "ubnt-tftp"
    assert method_for("ubnt_rocket-m").key == "ubnt-tftp"
    for ac in ("ubnt_nanostation-ac", "ubnt_litebeam-ac-gen2", "ubnt_rocket-5ac-lite", "ubnt_lap-120",
               "ubnt_powerbeam-5ac-300", "ubnt_nanobeam-2ac-13", "ubnt_bullet-ac"):
        assert method_for(ac).key == "ubnt-ac", ac
    assert method_for("ubnt_airrouter").device_ip == "192.168.1.1"
    assert METHODS["ubnt-tftp"].device_ip == "192.168.1.20"           # not changed by the copy
    assert method_for("glinet_gl-ar300m16").key == "openwrt-web"
    assert method_for("cudy_tr3000-v1").key == "cudy"
    assert method_for("openwrt_one").key == "openwrt-one"
    assert method_for("x86_64", "x86/64").key == "x86"
    assert method_for("morse_halowlink1", "", ["MorseMicro HaLowLink 1"]).key == "luci"
    assert method_for("someone_new").key == "unknown"


def test_images_picked_from_what_aredn_publishes():
    mk = METHODS["mikrotik-pxe"]
    use, other = pick_images(mk, ["kernel", "sysupgrade", "sysupgrade-v7"])
    assert use == ["kernel", "sysupgrade"] and other == ["sysupgrade-v7"]
    assert pick_images(METHODS["tplink"], ["factory", "sysupgrade"]) == (["factory"], ["sysupgrade"])
    assert pick_images(METHODS["unknown"], ["factory", "sysupgrade"]) == (["factory", "sysupgrade"], [])


def test_files_staged_with_the_names_devices_ask_for(tmp_path):
    k = tmp_path / "aredn-x-initramfs-kernel.bin"
    k.write_bytes(b"kernel")
    s = tmp_path / "aredn-x-sysupgrade.bin"
    s.write_bytes(b"sys")
    files = stage_files([("kernel", k), ("sysupgrade", s)], METHODS["mikrotik-pxe"], tmp_path / "out")
    assert files[0]["as"] == "rb.elf" and Path(files[0]["as_path"]).read_bytes() == b"kernel"
    assert files[1]["as"] is None and Path(files[1]["path"]).exists()
    f = stage_files([("factory", s)], METHODS["tplink"], tmp_path / "tp")
    assert (tmp_path / "tp" / "recovery.bin").exists() and f[0]["as"] == "recovery.bin"


def test_every_method_ends_by_finding_the_node_or_points_to_the_guide():
    for m in METHODS.values():
        assert m.steps, m.key
        if m.key not in ("x86", "unknown"):
            assert m.steps[-1].auto == "probe" or any(s.auto == "probe" for s in m.steps), m.key
