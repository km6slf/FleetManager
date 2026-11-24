"""First-time AREDN installs ("New device" wizard).

How each kind of hardware gets AREDN for the first time, from AREDN's own install guide
(https://arednmesh.readthedocs.io/en/latest/arednGettingStarted/installing_firmware.html).
Which method applies is decided from the hardware's profile in AREDN's catalog (its id,
target and vendor); which image files it needs comes from that profile's image list.

This module only describes the steps and picks files. Steps marked ``auto`` are ones a later
build will perform itself (TFTP push, PXE boot server, web upload); until then the wizard
shows how to do them by hand.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

GUIDE = "https://arednmesh.readthedocs.io/en/latest/arednGettingStarted/installing_firmware.html"


@dataclass
class Step:
    title: str
    text: str
    auto: str = ""           # "tftp" | "pxe" | "upload" | "probe": what a later build automates
    warn: str = ""


@dataclass
class Method:
    key: str
    name: str
    summary: str
    images: list[str]                          # image types needed, in order of use
    rename: dict[str, str] = field(default_factory=dict)   # type -> name the device asks for
    pc_ip: str = ""
    device_ip: str = ""
    steps: list[Step] = field(default_factory=list)
    risk: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


SWITCH_TIP = ("Put a plain (unmanaged) Ethernet switch between this computer and the device, "
              "so this computer's link stays up while the device reboots.")
DHCP_BACK = ("Set this computer's Ethernet back to automatic (DHCP). A freshly installed AREDN "
             "node hands out an address and answers at http://192.168.1.1 (it starts as a "
             "first-boot node you then set up).")

METHODS: dict[str, Method] = {
    "ubnt-tftp": Method(
        "ubnt-tftp", "Ubiquiti 802.11n: TFTP",
        "The device's built-in recovery accepts the AREDN factory image over TFTP.",
        ["factory"], pc_ip="192.168.1.10 / 255.255.255.0", device_ip="192.168.1.20",
        steps=[
            Step("Set this computer's address", "Give this computer's Ethernet the static address "
                 "192.168.1.10, mask 255.255.255.0. " + SWITCH_TIP),
            Step("Start the device in recovery mode",
                 "Hold the device's reset button while plugging its cable into the PoE adapter's "
                 "POE port. Keep holding for about 30–45 seconds, until the LEDs alternate in a "
                 "1-3, 2-4 pattern, then release."),
            Step("Send the factory image", "Push the factory image to the device by TFTP. On Windows: "
                 "tftp.exe -i 192.168.1.20 put <factory file> (turn on \"TFTP Client\" in Windows "
                 "Features first). Use 192.168.1.1 instead for an AirRouter.", auto="tftp"),
            Step("Wait for AREDN", "The device writes the image and reboots into AREDN by itself. "
                 + DHCP_BACK, auto="probe"),
        ]),
    "ubnt-ac": Method(
        "ubnt-ac", "Ubiquiti 802.11ac: SSH with a patched flasher",
        "The device must run airOS 8.7.0 (8.7.4 on newer LiteBeam/PowerBeam/NanoBeam AC). The "
        "factory image is copied over by SCP and written with a signature-bypassed flasher.",
        ["factory"], pc_ip="192.168.1.10 / 255.255.255.0", device_ip="192.168.1.20",
        risk="Advanced: follow AREDN's page for the exact commands. Don't unplug the device "
             "until it reboots.",
        steps=[
            Step("Set this computer's address", "Static 192.168.1.10, mask 255.255.255.0. " + SWITCH_TIP),
            Step("Check the airOS version", "In the device's web page at http://192.168.1.20, make "
                 "sure it runs airOS 8.7.0 (XC.V8.7.0, WA.V8.7.0 or 2WA.V8.7.0); newer LiteBeam, "
                 "PowerBeam and NanoBeam AC units need 8.7.4. Upload that airOS first if not."),
            Step("Create an admin login", "Create an admin account in the device's web page."),
            Step("Copy the image", "scp <factory file> admin@192.168.1.20:/tmp/factory.bin "
                 "(AREDN's page lists options for SSH negotiation errors)."),
            Step("Build the flasher and write the image",
                 "ssh admin@192.168.1.20, build /tmp/fwupdate.real with the hexdump command on "
                 "AREDN's page, chmod +x it, then run /tmp/fwupdate.real -m /tmp/factory.bin.",
                 warn="Do not remove power until the device reboots."),
            Step("Wait for AREDN", "The node comes up at 192.168.1.1. If it isn't reachable after "
                 "five minutes, power-cycle it. " + DHCP_BACK, auto="probe"),
        ]),
    "mikrotik-pxe": Method(
        "mikrotik-pxe", "MikroTik: network boot, then upgrade",
        "Two stages: boot AREDN's initramfs kernel over the network (named rb.elf), then install "
        "the sysupgrade image from the running AREDN.",
        ["kernel", "sysupgrade"], rename={"kernel": "rb.elf"},
        pc_ip="192.168.1.10 / 255.255.255.0", device_ip="192.168.1.1",
        steps=[
            Step("Set this computer's address", "Static 192.168.1.10, mask 255.255.255.0. The "
                 "addresses handed out in the next step must not include .10. " + SWITCH_TIP),
            Step("Start a network-boot server", "Serve rb.elf by BOOTP/DHCP + TFTP. On Windows, "
                 "AREDN's guide uses Tiny PXE: add rfc951=1 under [dhcp] in config.ini, copy rb.elf "
                 "into its files folder, set Option 54 to this computer's address, tick Bind IP, "
                 "set Filename to rb.elf, untick the iPXE user-class filename option, click Online.",
                 auto="pxe"),
            Step("Network-boot the device", "With the device powered off, hold reset while powering "
                 "it on, for about 20–30 seconds, until the server shows rb.elf was sent "
                 "(Tiny PXE: \"TFTPd: DoReadFile: rb.elf\"). Release, then stop the server."),
            Step("Find the temporary AREDN", "The device now runs AREDN from memory at 192.168.1.1. "
                 "On hAP ac units, move the cable from port 1 (WAN) to a LAN port (2–4) first.",
                 auto="probe"),
            Step("Install for good", "Open http://192.168.1.1, choose the sysupgrade file and click "
                 "\"Upload & Reboot\".", auto="upload"),
            Step("If network boot won't start", "In RouterOS run "
                 "/system/routerboard/settings/set boot-device=try-ethernet-once-then-nand "
                 "(RouterOS Device Mode may need changing first)."),
        ]),
    "tplink": Method(
        "tplink", "TP-Link CPE/WBS: web upload or network recovery",
        "Either upload the factory image in TP-Link's PharOS web page, or serve it as "
        "recovery.bin to the device's network recovery.",
        ["factory"], rename={"factory": "recovery.bin"},
        pc_ip="192.168.0.100 / 255.255.255.0 (network recovery)",
        steps=[
            Step("Option 1: PharOS web page", "If the model's PharOS system page offers a firmware "
                 "upload, upload the factory image there."),
            Step("Option 2: set this computer's address", "Static 192.168.0.100, mask 255.255.255.0. "
                 + SWITCH_TIP),
            Step("Option 2: start a network-boot server", "Serve recovery.bin (Tiny PXE as for "
                 "MikroTik with Filename recovery.bin; a plain TFTP server may also work).",
                 auto="pxe"),
            Step("Option 2: recovery boot", "Hold reset while powering the device on, for about "
                 "20–30 seconds, until the server shows recovery.bin was sent."),
            Step("Wait for AREDN", "The device installs AREDN and reboots. " + DHCP_BACK, auto="probe"),
        ]),
    "openwrt-web": Method(
        "openwrt-web", "GL.iNet: upload in its web page",
        "GL.iNet routers run OpenWrt; upload AREDN's sysupgrade image in their web page.",
        ["sysupgrade"], device_ip="192.168.8.1",
        steps=[
            Step("Connect", "Plug into the router; it gives this computer an address. Its page is "
                 "at http://192.168.8.1. (Some AR300M16 units have a boot_dev switch: check GL.iNet's "
                 "boot notes first.)"),
            Step("Upload", "Upgrade > Local Upgrade: untick Keep Settings, upload the sysupgrade file.",
                 auto="upload"),
            Step("Wait for AREDN", DHCP_BACK, auto="probe"),
        ]),
    "cudy": Method(
        "cudy", "Cudy: Cudy's OpenWrt first, then AREDN",
        "Cudy's own page only accepts Cudy-signed images, so first install Cudy's intermediate "
        "OpenWrt image (from Cudy, not AREDN), then AREDN from OpenWrt.",
        ["sysupgrade"], device_ip="192.168.10.1",
        steps=[
            Step("Install Cudy's OpenWrt", "At http://192.168.10.1 set an admin password, then "
                 "General Settings > Firmware > Local Update: upload Cudy's intermediate OpenWrt "
                 "sysupgrade image (e.g. cudy_tr3000-v1-squashfs-sysupgrade.bin from Cudy)."),
            Step("Open OpenWrt", "Reconnect after the reboot; browse to http://192.168.1.1, log in as "
                 "root with no password."),
            Step("Install AREDN", "System > Backup/Flash Firmware > Flash Image: upload AREDN's "
                 "sysupgrade file with \"Keep settings\" unticked.", auto="upload"),
            Step("Wait for AREDN", DHCP_BACK, auto="probe"),
        ]),
    "openwrt-one": Method(
        "openwrt-one", "OpenWrt One: LuCI upload",
        "Set the boot switch to NAND, then flash AREDN's sysupgrade image in LuCI.",
        ["sysupgrade"], device_ip="192.168.1.1",
        steps=[
            Step("Boot from NAND", "Set the boot-mode switch to NAND."),
            Step("Open LuCI", "Browse to http://192.168.1.1; a new unit has user root with an empty "
                 "password."),
            Step("Flash", "System > Backup/Flash Firmware > Flash new firmware image: choose the "
                 "sysupgrade file, untick \"Keep settings\", Continue.", auto="upload"),
            Step("Wait for AREDN", DHCP_BACK, auto="probe"),
        ]),
    "luci": Method(
        "luci", "OpenWrt-based (MorseMicro HaLow and similar): LuCI upload",
        "Flash AREDN's sysupgrade image from the device's OpenWrt web page.",
        ["sysupgrade"],
        steps=[
            Step("Open the device's page", "Browse to http://192.168.x.1, where x is the subnet this "
                 "computer was given; log in with the device's default credentials."),
            Step("Flash", "System > Backup/Flash Firmware > Flash new firmware image: untick "
                 "\"Keep settings\", choose the sysupgrade file, Flash image.", auto="upload"),
            Step("Wait for AREDN", DHCP_BACK, auto="probe"),
        ]),
    "zyxel": Method(
        "zyxel", "Zyxel: web configurator",
        "Upload AREDN's factory image as a firmware package in Zyxel's web configurator.",
        ["factory"], pc_ip="192.168.1.10 / 255.255.255.0", device_ip="192.168.1.2",
        steps=[
            Step("Set this computer's address", "Static 192.168.1.10 (any 192.168.1.x)."),
            Step("Log in", "http://192.168.1.2, user admin, password from the device label; choose "
                 "Standalone mode."),
            Step("Upload", "Configuration > Maintenance > File Manager > Firmware Package: upload the "
                 "factory file.", auto="upload"),
            Step("Wait for AREDN", "It reboots to 192.168.1.1. " + DHCP_BACK, auto="probe"),
        ]),
    "x86": Method(
        "x86", "x86 / virtual machine",
        "See AREDN's How-To guide, Virtual Machine Installs.", ["combined"],
        steps=[Step("Follow AREDN's VM guide", "AREDN's How-To Guide has the steps for each "
                    "hypervisor.")]),
    "unknown": Method(
        "unknown", "See AREDN's install guide",
        "Fleet Manager doesn't have steps for this hardware yet.", [],
        steps=[Step("Follow AREDN's guide", "Use the install guide for this device; the files "
                    "below are the ones AREDN publishes for it.")]),
}

_AC = re.compile(r"(^|[-_])(ac|[0-9]ac)([-_]|$)|lap-120|litebeam-ac|bullet-ac|rocket-5ac|powerbeam-5ac|nanobeam-2ac")


def method_for(profile_id: str, target: str = "", titles: list[str] | None = None) -> Method:
    pid = profile_id.lower()
    vendor = " ".join(titles or []).lower()
    if pid.startswith("ubnt_"):
        if _AC.search(pid):
            return METHODS["ubnt-ac"]
        m = METHODS["ubnt-tftp"]
        if "airrouter" in pid:
            return Method(**{**asdict(m), "steps": [Step(**s) for s in asdict(m)["steps"]],
                             "device_ip": "192.168.1.1"})
        return m
    if pid.startswith("mikrotik_"):
        return METHODS["mikrotik-pxe"]
    if pid.startswith("tplink_"):
        return METHODS["tplink"]
    if pid.startswith("glinet_"):
        return METHODS["openwrt-web"]
    if pid.startswith("cudy_"):
        return METHODS["cudy"]
    if pid == "openwrt_one":
        return METHODS["openwrt-one"]
    if pid.startswith("zyxel_"):
        return METHODS["zyxel"]
    if target.startswith("x86"):
        return METHODS["x86"]
    if re.search(r"morse|heltec|alfa|halow", pid + " " + vendor):
        return METHODS["luci"]
    return METHODS["unknown"]


def pick_images(method: Method, image_types: list[str]) -> tuple[list[str], list[str]]:
    """(types to use, other types AREDN publishes for this hardware). For methods without
    steps, every published image is offered."""
    if not method.images:
        return list(image_types), []
    use = [t for t in method.images if t in image_types]
    return use, [t for t in image_types if t not in use]


def stage_files(images: list[tuple[str, Path]], method: Method, dest: Path) -> list[dict[str, Any]]:
    """Copy the images into an install folder, adding copies under the names devices ask for
    (rb.elf, recovery.bin). Returns a file list for the wizard."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for kind, path in images:
        target = dest / path.name
        if not target.exists() or target.stat().st_size != path.stat().st_size:
            shutil.copy2(path, target)
        entry = {"type": kind, "name": path.name, "path": str(target), "as": None}
        if kind in method.rename:
            renamed = dest / method.rename[kind]
            shutil.copy2(path, renamed)
            entry["as"] = method.rename[kind]
            entry["as_path"] = str(renamed)
        out.append(entry)
    return out
