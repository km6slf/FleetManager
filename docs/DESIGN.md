# AREDN Fleet Manager — Design (v0.1)

Safe, bulk updating of AREDN nodes. Inspired by the "Fleet Manager … doesn't exist yet" slide from Pacificon 2026.

## Goals for v1
1. **Discovery** — map every reachable node and link, with firmware, model, and board ID per node.
2. **Credentials** — try several passwords and SSH keys per node, and remember which one works.
3. **Selection** — pick target nodes from a checklist (web UI) or a node list (CLI).
4. **Firmware upgrades** — upgrade from the edge inward, verifying each wave before moving on.
5. **Radio settings** — channel, channel width, and operating mode, applied without stranding nodes.

## Architecture
```
 ┌───────────── host (Linux / Pi / VM on the mesh) ─────────────┐
 │  aredn-fleet (pip package)                                   │
 │   discovery ─ topology/planner ─ job runner ─ health checks  │
 │   credentials store      web UI (FastAPI)      CLI           │
 └───────────────┬──────────────────────────────────────────────┘
                 │ HTTP sysinfo.json (read)   SSH :2222 root (write)
        ┌────────┴─────────┐
        │  AREDN nodes     │  optional: aredn-fleet-agent .ipk
        └──────────────────┘
```
* **Host package (controller).** All the logic lives here. It needs nothing installed on the nodes, so it works on any node that has SSH.
* **Optional node agent (.ipk).** A small ucode/shell helper that offers a stable, versioned command interface (`fleet-agent status|stage|apply|revert`). Its main job is the **timed apply with automatic revert** used for radio changes, which has to run *on the node* because the node may lose contact with the manager during the change. Without the agent, the controller pushes an equivalent one-shot script over SSH.

## Discovery
* Crawl `http://<node>.local.mesh/cgi-bin/sysinfo.json?link_info=1` breadth-first from `localnode`. No credentials are needed.
* The edge type comes from `linkType` (RF, DTD, TUN, …). Tunnels can be left out of planning (`--exclude TUN`) when they shouldn't be trusted during a rollout.
* Planned: cross-check against Babel's routing table over SSH, and keep traceroute (`traceroute` on the node, or from the host) as a diagnostic for links that disagree.

## Firmware catalog
* Source: AREDN Firmware Selector metadata on `downloads.arednmesh.org` (or a local rsync mirror via `--mirror`).
  * `afs/www/config.js` holds the `versions` map, with every release and nightly mapped to a data directory.
  * `afs/www/data/<ver>/overview.json` holds the hardware profiles (`id`, `target`, `titles`).
  * `afs/www/data/<ver>/<target>/<id>.json` holds `image_path`, `supported_devices` (OpenWrt board names) and `images` (`type`, `name`, `sha256`, `size`).
* Default channels are the **2 newest stable releases plus the newest nightly**. Both counts are configurable (`--stable N`, `--no-nightly`).
* `aredn-fleet firmware fetch` downloads the sysupgrade image for **every hardware profile** in each channel. `--include-factory` adds first-install images, and `--profiles` narrows the set.
* Every image is checked against its published SHA-256 before it's kept. Bad downloads are discarded, and cached files are re-verified rather than downloaded again.
* Cache layout is `~/.cache/aredn-fleet/firmware/<version>/<target>/<image>`, with a `manifest.json` per version.
* Nightly images live at `snapshots/targets/...` and are overwritten every night, so a stale nightly catalog fails its hash check instead of installing the wrong build. Fetch again before using a nightly.
* Node-to-image matching uses `/tmp/sysinfo/board_name` against `supported_devices`. The sysinfo model string is only a fallback, and the job refuses to run unless exactly one profile matches.

## Rollout planning (firmware)
* `root` is the node the manager reaches the mesh through.
* `dependents(v)` is the set of nodes that become unreachable from root while v is down. It is computed by removing v and running a BFS.
* A node becomes **ready** once all of its *selected* dependents are done. Ready nodes are ordered by hop distance, farthest first. Root always goes last.
* **Waves** (bounded by `max_parallel`) admit a node only if both of these hold:
  * taking the wave down together cuts nothing beyond what each member's own dependents already account for (this catches joint cuts);
  * every member stays reachable while the rest of the wave is down, so each one can be verified right away.
* Warnings are raised when a selected node is the only path to an unselected node. Unreachable selected nodes are skipped and reported.

### Per-node upgrade steps
1. Pre-flight: connect (trying each credential in turn), read the board name, current firmware and free RAM, resolve exactly one profile, and skip the node if it's already on the target version.
2. Stream the image to `/tmp` over SSH (`cat >`, since there's no SFTP dependency), then run `sha256sum` on the node to confirm it.
3. Run **`/usr/local/bin/aredn_sysupgrade`**. Plain `sysupgrade` must not be used on current AREDN.
4. Wait for the node to drop and come back (timeout).
5. Verify: `firmware_version` matches the target, the node's expected neighbors are back with acceptable link quality, and all of its dependents are reachable again.
6. Soak for an interval (TBD, configurable; default proposal 2–5 min per wave) before the next wave.
7. On failure, stop the rollout. Do **not** continue inward past a failed node.

### Release-candidate safeguards (from AREDN's RC notes)
These come from `aredn_fleet/rc.py`. They apply when the target is an RC nightly (20261012 or later) or a stable release after 4.26.10.0.
* **Config backup before every flash** (all upgrades, not just RC). The node writes `aredn.configuration.backup()` to `/tmp`, the manager downloads it over SSH, and saves it to `config/backups/<node>/<node>-<time>-<from-version>.tgz` (named for the node, so a copied file still says where it came from). If the backup fails, that node isn't upgraded. The backup can include the password hash and Wi-Fi keys, so it stays on this computer.
* **PtMP access point before its Mesh Stations.** Access point / station pairs come from the radio details (RF neighbours with Mesh PtMP and Mesh Station radios on the same band). The planner uses `must_precede` constraints so an access point is ordered ahead of its stations when both are selected; nodes behind them still go outermost first. This applies to every upgrade, since it's harmless.
  * For RC targets, a live check before each station: each access point must already run 4.26.7.0 or the RC, or be earlier in the same run. Otherwise the station is BLOCKED and the reason says what to upgrade first.
  * If a neighbour's radio details are missing, every RF neighbour is treated as a possible access point, and the message says so.
* **Removed AC channels.** For RC targets, a live read of the node's radios before flashing. Any 802.11ac 5 GHz radio (one that offers 80 MHz) whose channel and width overlap 48, 52, 56 or 64 is BLOCKED; for example 60 at 40 MHz overlaps 64.
* **IPQ40xx reboot freeze.** `ipq40xx` hardware upgrading *from* 4.26.7.0 gets a review warning that it may need one power cycle. If it doesn't come back, the failure says this is the likely cause.
* **Radio details after an upgrade.** They're re-read when an upgrade succeeds, and stored with the firmware they were read on. Details read on other firmware are marked out of date and excluded from radio choices until read again.

### Older firmware and the OLSR → Babel move
From AREDN's release notes: up to 3.25.4 nodes speak only OLSR; 3.25.5.0–3.25.10.0 speak OLSR and Babel side by side; 4.26.1.0 and later speak only Babel. Babel-only nodes don't know OLSR-only nodes exist. Exception: tiny builds (32 MB hardware such as the NanoStation M XM, last release 3.25.5.1) have no Babel on any release, so they stay OLSR-only and can never join a Babel-only mesh; Fleet Manager recognises them by their newest release being below 4.0 (from the release catalog) and labels them OLSR. (`aredn_fleet/routing.py`)

Status reads try `/a/sysinfo` first, then `/cgi-bin/sysinfo.json`, then port 8080. 3.25.x answers `/a/sysinfo` with HTTP 200 and an HTML error page, so a reply that isn't JSON counts as "try the next path" (found on the bench NanoStation after its upgrade).
* **Routing per node** comes from its firmware version, shown as an **OLSR** / **OLSR+Babel** badge in the Nodes table plus **Babel** (date-named nightlies before 2026-01-14 show "routing?"). During an upgrade the running daemons (`pidof olsrd`, `pidof babeld`) are checked too.
* **Migration rule** for a Babel-only target, judged on the mesh as it will be after the run, repeated until stable because a blocked node stays where it is:
  * an OLSR-only node is blocked: it must go to an OLSR + Babel release first;
  * a node is blocked if, afterwards, nothing on its path back to this computer speaks Babel;
  * a node is blocked if OLSR-only nodes reach an OLSR + Babel "bridge" only through it (the reason names them). Only the last bridge in front of them is held back.
  Moving to an OLSR + Babel release is never blocked by this.
* **Last release per hardware:** AREDN drops hardware (the 32 MB `ath79/tiny` units, e.g. the NanoStation M XM, stop at **3.25.5.1**). `ReleaseIndex` searches every stable release's catalog for the node's board name (cached on disk; stable releases don't change). If the chosen release has no image for a node, the review says which release is its last. The Firmware tab can download one specific release.
* **Older status page:** api_version 1.x at `/cgi-bin/sysinfo.json` (port 80, or 8080 on the oldest), with numbers as text and `link_info` as `[]` when empty. Discovery reads both formats.
* **Older upgrade procedure**, told apart by the node's own `aredn_sysupgrade` (newer ones archive the settings themselves). For older ones Fleet Manager does what the node's Firmware page does with "Keep settings":
  1. archive every existing path in `/etc/arednsysupgrade.conf` into `/tmp/arednsysupgradebackup.tgz` (a copy is saved on this computer as the backup);
  2. check the image with `validate_firmware_image` and the node's `firmwarecheck.sh` after dropping caches;
  3. detached: set `nodeupgraded` with the node's own Lua (`aredn.info.set_nvram`), stop the services listed in the node's `upgrade_prepare.sh` except dropbear (so a failure can still be read), then `aredn_sysupgrade -f <archive> -q <image>`. On this firmware a successful hand-off still *returns*: procd (PID 1) execs the stage-2 upgrader, cutting off sysupgrade's ubus call ("Connection failed", 246). So the flag and services are restored only if PID 1 is still `procd` a few seconds later (the first real run undid a good hand-off and deleted the image stage 2 needed; the node rebooted unchanged);
  4. wait through the second reboot older firmware does to apply the kept settings, then verify as usual.
* **Settings on older nodes** go through the node's own **Advanced Configuration** page (`/www/cgi-bin/advancedconfig`, Lua). Its `settings` table gives each entry's key (uci option), type, condition, callbacks and `needreboot`. Showing the page (a plain GET over SSH) lists only the settings whose condition holds on that node, with current values. Saving POSTs `button_save_<n>` / `newval_<n>` to the same page, so the node runs its own callbacks (e.g. `node-setup -a mesh`) exactly as its Save button would; the page is read back to confirm. Only real on/off uci settings are offered: text fields, actions (`aredn.olsr.restart`) and the dangerous-upgrade switch never are. Rows merge with newer firmware by setting name (e.g. `lan_dhcp_route`). When the page flags a reboot, the review says so and the result lists the nodes to reboot (Fleet Manager doesn't reboot them).
* On every firmware, switches that could cut Fleet Manager off (`ssh_access`) are shown but not changed in bulk.
* Radio changes need the ucode web UI (3.25.5+). Older nodes can still be **read** (view only): the mesh radio from `/etc/config.mesh/_setup`, its channels from `aredn.hardware.get_rfchannels` and widths from `get_rfbandwidths`, as their Basic Setup page does; a second radio reports its purpose only.
* **Field check (2026-11-24):** NanoStation M2 XM on 3.23.8.0 (Lua UI, `olsrd` with jsoninfo on 9090, DtD on IPv4 `/8`, IPv6 off, 28 MB RAM / 13.8 MB `/tmp`). Cabled to the Babel-only hAP's DtD port, neither saw the other: the hAP's DtD bridge has no IPv4 address.

## Radio settings (channel / width / mode)
Changing an RF node's channel, width, or mode cuts its RF links to every neighbor still on the old settings, so working edge-inward one node at a time would strand each node as it goes. Instead, each **RF cluster** switches together.

How AREDN 4.26 stores and applies radio settings (read from a bench node):
* Settings live in `/etc/config.mesh/setup` (uci `setup.globals`): `radioN_mode`, `radioN_channel`, `radioN_bandwidth`, `radioN_peer`, `radioN_txpower`, `radioN_distance`.
* The modes are `mesh` (ad-hoc), `meshap` (PtMP AP), `meshptp`, `meshsta` (PtMP station), `lan`, `wan` and `off`.
* The web UI applies changes through the ucode module `aredn.configuration`: `prepareChanges()` → `setSetting()` → `saveSettings()` → `commitChanges()`. The last step runs `node-setup` and then `restart-services`, and marks `/tmp/reboot-required` if a reboot is needed. The fleet manager uses the **same calls**.
* `aredn.radios.getConfiguration()` gives each radio's channel list for each width (the same data the node's dialog uses), and the manager validates every requested change against it. See **Radios (per band)** below.
* `node-setup` derives the mesh SSID from the width (`AREDN-<bw>-v3`); for PtMP and PtP it also uses the channel. Nodes on different widths can't link at all.

The job (`aredn-fleet radio`):
1. **Preflight (read-only):** re-read every radio over SSH, refuse anything the node's own dialog wouldn't allow, and refuse if a node's clock is more than 20 s off. Dry run stops here.
2. **Arm:** upload a script to every node in the cluster.
   * The script backs up `setup` to flash (`/etc/aredn-fleet-radio/`) and installs a boot hook in `/etc/rc.local`, which is in the sysupgrade keep list.
   * It sleeps until a **shared switch time**, applies the change through `aredn.configuration`, and reboots if AREDN flags that a reboot is required.
   * A revert timer, counted from boot so it doesn't depend on NTP, **restores the backup and reboots unless the change is confirmed** within `revert_after` (default 600 s).
   * If arming fails on any node, the others are cancelled before the switch.
   * Preflight refuses nodes that don't run `rc.local` at boot.
3. **Verify:** for each changed radio, `iw dev <iface> info` must show the new frequency and interface type, and the node's RF neighbours must be back.
4. **Confirm all or none:** if every node verifies, touch `confirmed` on each. Otherwise confirm none, and the whole cluster reverts together instead of splitting.
* Clusters are separated by DTD and tunnel links and are changed one at a time. The job stops after the first cluster that fails.
* It warns when an unselected RF neighbor would lose its link.
* Known limitations:
  * If a confirm fails partway through, the nodes already confirmed stay changed.
  * Writes about 2 KB to flash per change (the backup plus the rc.local line), and removes it on confirm, cancel or revert.
* **4.26.1.0 detail:** in `node-setup`, the `case "wireless":` branch has no `break`, so it falls through to `default: changes.reboot = true`. As a result, **any wireless change, channel included, needs a reboot**, and `restart-services` exits without reloading anything. The job handles both cases: a reboot, or a plain `restart-services` if a later firmware fixes this.
* The script's revert, confirm and reboot paths run under a POSIX shell with stubbed AREDN tools in CI (`tests/test_radio_script_sim.py`).

## Radios (per band)
* **Inventory:** "Read radios" logs in over SSH and runs `aredn.radios.getConfiguration()` on each node. That returns every radio: interface, band, mode, channel, width, supported widths, the channel table per width with the node's own labels, and `managedOOB`. **WAN-client SSIDs and keys are dropped when read and never stored.** Each node is also probed for whether it can mesh on two radios at once.
* **Bands:** whatever band the node reports, shown as 900 MHz, 2.4 GHz, 3.4 GHz, 5 GHz or 6 GHz. If a node doesn't name a band, it's inferred from frequency (6 GHz from 5925 MHz up). Bands are sorted low to high.
* **Dropdowns:** built exactly like the node's Radios & Antennas dialog. The list is `channels[width]` with the node's labels, and `managedOOB` channels are hidden in Mesh PtMP, Mesh PtP and Mesh Station modes. For a multi-node selection, each band offers only the widths and channels every selected node with that band supports. Nodes without that band are left alone.
* **Changes** are per band (`BandChange`) and resolved per node to `radioN_*` settings. A radio switching into a mesh mode gets SSID `AREDN`, as the dialog does. The job refuses to:
  * change WAN Client or LAN Hotspot radios,
  * run two mesh radios on firmware that can't,
  * pick a channel the node's dialog wouldn't offer,
  * act on a node with two radios on the same band.
* **Verification** after the switch is live:
  * `iw dev <iface> info` must show the expected frequency and interface type. Widths of 5 and 10 MHz are driver settings that iw reports as 20, so width is only checked at 20 MHz and up.
  * Every RF neighbour the node had before must be back.
* **Mesh SSID:** `radioN_ssid` holds only the base name (default `AREDN`), and `node-setup` adds the rest:
  * `<base>-<width>-v3` for Mesh,
  * `<base>-<channel>-<width>-v3` for Mesh PtMP, PtP, Station, and Mesh on 900 MHz.

  Groups on a non-standard SSID, such as IDEC, can set the base group-wide or per node, and the GUI previews the full SSID. Base names are limited to 1–20 letters, digits, `-`, `_` and `.`.
  * A mesh radio's SSID is **never reset** unless asked. If it can't be read, it's left alone rather than "restored" to `AREDN`.
  * A radio entering a mesh mode gets `AREDN`, or the requested base. It never keeps a former WAN client's network name.
  * Verification checks the broadcast SSID with `iw`.
* See `docs/NIGHTLY.md` for the release-candidate features: multi-radio mesh, multi-channel stations, internet bypass and more.

## Node settings (learned from each node)
Fleet Manager doesn't carry a list of AREDN settings: they change between releases. Instead it reads each node's own web dialogs (`/app/main/status/e/*.ut`, every template that has a switch) and parses them (`jobs/settings.py`):
* **What the node shows:** each switch's label, short description and help text.
* **What it reads:** the uci option in `/etc/config.mesh` and the rule: `!== "0"` (on unless 0), `=== "1"`, or `=== "0"` (inverted, like the WAN firewall switch reading `passthrough`).
* **When it's disabled:** a `disabled:` variable defined in the same file from another option (e.g. `wan_passthrough`).
* **What saving writes:** the save handler's `if ("<name>" in request.args) { … }` block. Only a block that sets exactly one option, as on/off, is changed in bulk. Anything else (the WAN firewall switch also turns three other settings off) is listed with the reason and left to the node's dialog.
* Definitions depend only on firmware, so they're cached per firmware version; values are read per node with `uci -c /etc/config.mesh get`. Option names that look like secrets are never read or written.
* **Applying** uses the dialog's own steps: `aredn.configuration.prepareChanges()`, `uci.cursor("/etc/config.mesh").set/commit`, `saveSettings()`, `commitChanges()` (node-setup decides what restarts or whether a reboot is needed). It runs detached, since networking may restart, then the values are read back. Nodes go one at a time, outermost first, the node this computer connects through last, stopping at the first failure. Same review-then-execute rule as upgrades (typed `apply`, bound to the reviewed dry run).
* **GUI:** the "Node settings" card lists switches in the order the nodes show them, with the nodes' labels (several labels if firmware differs), counts on/off/not present/disabled, and Leave/On/Off.
* Radio purposes come from the node too: `hardware.supportsFeature("wifi-mode", iface, mode)` per radio, as the Radios & Antennas dialog asks. Unsupported purposes aren't offered and are refused. Firmware that doesn't answer gets no restriction from this. The two-radio probe also asks `supportsFeature("only_one_radio")`.

## Node-specific settings
* Any node can have its own radio settings per band: purpose, width, channel, SSID base and peer, plus a note saying why. They're stored in `config/overrides.json`, separate from discovery, so rediscovering doesn't lose them.
* The node **stays in the group**: for firmware upgrades, and for radio bands where it has no own settings. **On a band with own settings, group radio changes don't apply to that node.** Its own values are applied, and every field left blank **keeps the node's current value**, not the group's (`merge_changes`). Own settings are (re)applied whenever the node is part of a radio job.
* The settings are applied idempotently: a node already set that way is "unchanged", so a job with no group change can be run just to apply node settings.
* Saving checks node settings against the node's own radios, with the same rules as a real change. Saving requires the node's radios to have been read.
* **Expected links:** each radio's on-air signature after the change is band, link type (ad-hoc / PtMP / PtP), channel, width and full SSID. Two nodes keep an RF link only if they share a signature.
  * Links a node's own settings deliberately drop are listed in the dry run ("WILL LOSE RF LINK TO …").
  * The job doesn't wait for those links, so an intended split never triggers a revert. It still requires every link that *should* exist.
* **GUI:**
  * a purple **custom** marker next to the node name, with a tooltip summarising its settings and note;
  * an **Own settings** column with Set… / Edit;
  * an editor that reuses the band controls with the node's own lists, where blanks read "Keep current (…)" with the node's actual value;
  * the group radio panel names the nodes on each band that have own settings, and says the group change won't apply to them;
  * "· own settings" on that node's line in the radio panel;
  * the confirm dialog lists every node that will use its own settings.

## Field findings — bench node (MikroTik hAP ac lite, AREDN 4.26.1.0)
* Board name is `mikrotik,routerboard-952ui-5ac2nd` (from `/tmp/sysinfo/board_name`) and the target is `ath79/mikrotik`.
* RAM is tight: 57.6 MB total, about 20 MB available, and `/tmp` (tmpfs) has 27.8 MB free. That's enough for one ~8 MB image, but the RAM preflight check stays.
* **`aredn_sysupgrade [--firstuse] [--force] <file>`**:
  1. It first validates the image with `platform_check_image` and `/usr/libexec/validate_firmware_image`, checking both the signature and the device match.
  2. Next it **stops babel**, writes a config backup (`aredn.configuration.backup` → `/tmp/arednsysupgradebackup.tgz`) and stops the manager.
  3. Finally it runs `/sbin/sysupgrade -f <backup> -q`.
  * Settings are preserved. The fleet manager never passes `--force`.
  * The manager now runs the same validator itself before launching, since a detached launch would lose the exit code.
  * After launch it checks the log for `ERROR:` while SSH still works. A failure after babel stops leaves the node off the mesh until it reboots, so the job stops and reports it.
* **Host keys persist across upgrades.** `/etc/dropbear/dropbear_*_host_key` and `authorized_keys` are in the sysupgrade keep list. Host-key pinning is now enabled: trust on first use, then pin in `~/.config/aredn-fleet/known_hosts`. A changed key refuses the connection.
* Radio config, as generated in `/etc/config/wireless` from `/etc/config.mesh/setup` by `node-setup`:
  * `radio0.channel='131'`, `chanbw='20'`, `htmode='VHT20'`, `band='5g'`, `distance`, `txpower`
  * mesh iface `mode='adhoc'` (IBSS), `ssid='AREDN-20-v3'`
  * The SSID appears to encode the channel width.
* Routing: `babeld` runs with a control socket at `/var/run/babel.sock`, and `socat` is installed. `arednlink` handles host and service distribution.
* The web UI is server-side ucode (`uhttpd -o /a -O /app/root.ut`). Library code is in `/usr/share/ucode/aredn/` (`configuration.uc`, `radios.uc`, `babel.uc`, `hardware.uc`).
* The node also runs telnet. Note this for the security section.
* **sysinfo moved:** `/cgi-bin/sysinfo.json` now returns a 307 redirect to `/a/sysinfo`. Discovery requests `/a/sysinfo` first and falls back to the old path.
* Babel's control socket (`echo dump | socat - UNIX-CONNECT:/var/run/babel.sock`) lists interfaces, neighbors and routes with their next hops. This can give exact hop paths without traceroute (planned).

## New device (first install)
A **New device** tab walks through putting AREDN on hardware for the first time (`aredn_fleet/install.py`), following AREDN's install guide (https://arednmesh.readthedocs.io/en/latest/arednGettingStarted/installing_firmware.html).
* Pick a release and search AREDN's hardware list for it (the catalog's `overview.json`; stable releases cached on disk).
* The install method comes from the hardware's catalog profile: Ubiquiti 802.11n (TFTP of the factory image to 192.168.1.20), Ubiquiti 802.11ac (airOS 8.7.0/8.7.4, SCP + patched flasher; marked advanced), MikroTik (network-boot the initramfs `kernel` image as `rb.elf`, then sysupgrade from 192.168.1.1), TP-Link CPE/WBS (PharOS upload, or the factory image as `recovery.bin` by network recovery with the PC at 192.168.0.100), GL.iNet, Cudy (Cudy's own OpenWrt first), OpenWrt One (NAND switch, LuCI), MorseMicro/HaLow-style LuCI, Zyxel, x86 (VM guide); anything else points to AREDN's guide with every image AREDN publishes for it.
* "Get the files" downloads the images that method needs (SHA-256 checked) into `config/install/<profile>-<release>/`, adds copies under the names devices ask for (`rb.elf`, `recovery.bin`), and lists any other images AREDN publishes for that hardware (e.g. MikroTik `sysupgrade-v7`) without guessing which applies.
* Steps show the PC and device addresses, a checkbox per step, "Show"/"Open folder" for the files (restricted to Fleet Manager's own folders), and "Look for the node", a read-only sysinfo check that finds the new node (192.168.1.1 by default).
* Planned automation, marked in the steps: a built-in TFTP push (Ubiquiti 802.11n), a built-in BOOTP/DHCP + TFTP network-boot server (MikroTik, TP-Link), and uploading the sysupgrade image to the temporary AREDN (MikroTik stage 2).
* Test hardware to order (one per method): TP-Link CPE210/510 v3, MikroTik hAP ac2, a used Ubiquiti XW unit; optionally a GL.iNet GL-AR300M16 or GL-MT1300.

## Credentials
* Credentials are kept in an ordered list (keys before passwords), and the inventory records which one worked for each node.
* Passwords come from environment variables, a prompt, or the OS keyring. They are never stored in the inventory file in plaintext.
* SSH runs on port 2222 as root and supports RSA, ECDSA, and Ed25519 keys.
* Planned: a job that pushes a fleet public key to nodes that still only accept a password.

## Interfaces
* **CLI** (`aredn-fleet`): `discover`, `plan`, `firmware`, `creds`, `upgrade`, `radio`, `gui`, `serve`.
* **GUI**: a local FastAPI server plus a static HTML/JS front end (no build step), shown in a native window by pywebview. That's WebView2 on Windows and WebKitGTK on Linux. The same server runs headless (`aredn-fleet serve`) for a Pi or VM on the mesh.
  * Tabs: **Nodes** (checklist, filters, only-path-to column, upgrade and radio panels), **Map** (layered by hop, RF/DTD/TUN links, click to select), **Firmware**, **Credentials**, **Activity** (job list, live log, Stop).
  * **Auth:** the server listens on 127.0.0.1 with a random per-run token (the launcher opens `/?token=…`, which sets an HttpOnly cookie). `serve` prints a link with the token. Every `/api` call is refused without it.
  * **Jobs** run in the background, and the UI polls for new log lines. Only one job that changes nodes may run at a time. Stop is cooperative: a rollout stops between waves and never in the middle of a flash.
  * **Confirmation:** a real upgrade or radio change requires typing `upgrade` or `switch`. The server checks this too, not just the UI.
  * The version picker defaults to the newest stable release, and nightlies are labelled "testing only".
  * Passwords added in the GUI go to the OS credential store (`keyring`). Keys are referenced by path.
* Every job is **dry run first**. It shows the wave plan and needs an explicit confirmation before it runs.

## Review before execute
* **Upgrade…** and **Switch…** first run a dry run, which logs in to every selected node read-only. They then show a **review ledger** built from its results:
  * **Upgrade:** wave by wave in execution order, one row per node with model, image, current → new firmware, and status (will upgrade / already current / BLOCKED with the reason). It also lists only-path warnings and unreachable nodes. The wave containing the node this computer connects through is labelled as going last.
  * **Radio:** RF cluster by RF cluster, all switching at one moment. For each node and changed radio band it shows before → after for purpose, channel (node's own labels), width and the full SSID. Unchanged values are greyed out. It marks nodes using their own settings, RF links dropped on purpose ("will lose RF link to … (intended)"), and unselected neighbours that will lose links.
* If any node is blocked, the typed confirmation and Go stay disabled.
* **What you review is what runs.** An execute must cite a dry-run job (`reviewed_job`) of the same kind that:
  * finished cleanly with no blocked nodes,
  * is under 15 minutes old,
  * has the same **fingerprint**.

  For upgrades the fingerprint covers nodes, version, parallelism, soak, the tunnel option and the root node. For radio changes it covers nodes, group changes, every selected node's own settings and the revert time. Otherwise the server answers 409 ("something changed since you reviewed the plan…").
* Execution still re-checks every node (preflight) before touching anything.

## Packaging
* **Windows** (`.github/workflows/release.yml`, on every push to main and on `v*` tags):
  1. pytest.
  2. PyInstaller one-folder bundle (`packaging/windows/fleet-manager.spec`).
  3. **`--selftest`**: starts the bundled server, checks the UI, API and imports, then exits. The build fails if it fails.
  4. **Inno Setup installer**: per-user, no admin, Start menu entry, optional desktop icon. Settings are kept on uninstall.
  5. **Portable zip**: the same folder plus `portable.txt`. Its presence switches all storage to `data\` beside the exe.
  6. Artifacts on every run; attached to a GitHub Release on tags.
* **Storage locations** (`paths.py`):
  * Windows: config in `%APPDATA%\aredn-fleet`, firmware cache in `%LOCALAPPDATA%\aredn-fleet`.
  * Linux: XDG directories.
  * Portable: `data\` beside the exe.
  * The old `~/.config/aredn-fleet` from the CLI builds is migrated on first run.
* **Linux .deb:** planned. It will be a venv under `/opt` using the system `python3-gi`, with a desktop entry plus an optional systemd service. See `packaging/linux/README.md`. A test PyInstaller build on Linux hit GTK binding mismatches, which is why the .deb uses a venv instead.

## Safety principles
* Never act on a node the manager can't verify afterwards.
* Stop on the first failure, and never work past it toward the root.
* Keep an audit log of every command sent to every node.
* Treat throttling and RF airtime as real limits: send firmware images with bounded parallelism.

## Status
* [x] Topology model and edge-inward wave planner, with tests
* [x] sysinfo.json crawler (tested with a fake fetcher)
* [x] Credential store, plus `creds add/list`
* [x] CLI: `discover`, `plan`, `firmware channels/fetch/list`, `upgrade`
* [x] Firmware catalog and downloader (2 stable + nightly, all hardware, SHA-256 verified)
* [x] SSH transport (asyncssh, port 2222) with credential probing
* [x] Firmware job: dry run by default, stops on first failure, soak and re-check between waves, JSONL audit log (tested against a simulated mesh)
* [x] GitHub Actions CI (Python 3.10, 3.12, 3.13)
* [x] SSH host-key pinning (TOFU + pin; keys confirmed to persist across sysupgrade)
* [x] `aredn_sysupgrade` arguments and settings preservation confirmed from source on 4.26.1.0
* [x] Windows install, discover, `firmware fetch --board`, upgrade dry run against the bench node: all good
* [x] Real upgrades on the bench node through the GUI: 4.26.1.0 → 4.26.10.0 → nightly 20261122 (after fixing the launch: BusyBox has no `nohup`)
* [x] Radio settings job (synchronized switch per RF cluster, on-node auto-revert via pushed script; simulated-cluster tests)
* [x] Per-band radio settings for 1- and 2-radio nodes (900 MHz, 2.4, 3.4, 5, 6 GHz), channel lists identical to the node's own dialog, multi-mesh probe
* [x] Editable mesh SSID (group or per node) with live preview; never reset implicitly
* [x] Node-specific settings that stay in the group, with list marker, editor, merge, and expected-link logic
* [x] Review ledger before execute (wave order / per-radio before→after), execution bound to the reviewed dry run
* [x] RC safeguards: pre-flash config backups, PtMP AP before stations, removed AC channels blocked, IPQ40xx freeze warning, radio re-read/out-of-date marking
* [x] Node settings learned from each node's own dialogs (labels, rules, save handler), bulk on/off job with review; radio purposes from the node's hardware module
* [x] OLSR-era nodes: old status page, routing badge, migration rule, last release per hardware, older upgrade procedure (tested against a simulated node; recon from a real 3.23.8.0 NanoStation)
* [x] Settings on older nodes through their own Advanced Configuration page (tested with the real 3.23.8.0 page)
* [x] Babel badge; backup files named for the node
* [x] New device wizard: hardware picker from AREDN's catalog, method per hardware, images downloaded and renamed, step-by-step guide, look-for-the-node check
* [ ] Wizard automation: TFTP push, network-boot server, stage-2 upload
* [x] First real upgrade of an older node (NanoStation M2 XM 3.23.8.0 → 3.25.5.1): flashed and kept its settings; fixes from it: stage-2 hand-off, `/a/sysinfo` HTML-200 fallback, tiny builds labelled OLSR
* [ ] Radio job on the bench node (needs 2 nodes for a real link test)
* [x] Nightly recon (3 passes): new channel-list format and setter names supported
* [ ] Multi-channel Mesh Stations (`radioN_channels`)
* [ ] Package the revert script as the optional .ipk agent
* [x] GUI (desktop window + headless serve), token auth, background jobs with stop; API tests; bundle self-test
* [x] Windows installer + portable zip built in CI
* [x] CI build installed and run on a real Windows machine (after fixing the no-console launch crash)
* [ ] Linux .deb (desktop + headless service)
* [ ] Field test on a lab mesh (2–3 spare nodes) before any production use
