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
* See `docs/NIGHTLY.md` for the release-candidate features: multi-radio mesh, multi-channel stations, internet bypass and more.

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
* [ ] First real upgrade on the bench node (4.26.1.0 → 4.26.10.0)
* [x] Radio settings job (synchronized switch per RF cluster, on-node auto-revert via pushed script; simulated-cluster tests)
* [x] Per-band radio settings for 1- and 2-radio nodes (900 MHz, 2.4, 3.4, 5, 6 GHz), channel lists identical to the node's own dialog, multi-mesh probe
* [ ] Radio job on the bench node (needs 2 nodes for a real link test)
* [ ] Nightly: run `tools/recon/recon-nightly.sh`, then multi-channel stations and the bulk settings job (internet bypass/sharing)
* [ ] Package the revert script as the optional .ipk agent
* [x] GUI (desktop window + headless serve), token auth, background jobs with stop; API tests; bundle self-test
* [x] Windows installer + portable zip built in CI
* [x] CI build installed and run on a real Windows machine (after fixing the no-console launch crash)
* [ ] Linux .deb (desktop + headless service)
* [ ] Field test on a lab mesh (2–3 spare nodes) before any production use
