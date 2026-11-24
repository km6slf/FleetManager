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
This is a different problem from firmware. Changing an RF node's channel cuts its RF links to every neighbor still on the old channel, so working edge-inward one node at a time would strand each node as it goes.

Plan:
1. Group the selected nodes into **RF clusters**: connected components that use only RF links, with DTD and tunnel links between clusters.
2. Within a cluster, **stage** the new settings on every node, applying edge-inward exactly as for firmware, but *don't* commit them yet.
3. **Synchronized commit:** every node in the cluster applies the change at the same scheduled time (all nodes keep NTP time on the mesh).
4. **Dead-man revert:** each node arms a revert timer (e.g. 10 min). If it can't see its expected neighbors on the new settings, it rolls back on its own. This runs on the node, through the agent or a pushed script.
5. The controller verifies the whole cluster on the new settings and then disarms the revert timers.
* Changing the operating mode (mesh / PtMP / PtP / station, etc.) follows the same staged, timed process, with extra checks for modes that change which node is the parent.
* Open item: confirm, per firmware version, which config keys and apply command AREDN 4.26.x uses for channel, width, and mode. Read the node's own setup code before writing anything.

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
* Open questions: the key names in `/etc/config.mesh/setup` for channel, width and mode, and the apply path after a change. Recon pass 2 reads `configuration.uc`, `radios.uc` and the UI's radio handlers.

## Credentials
* Credentials are kept in an ordered list (keys before passwords), and the inventory records which one worked for each node.
* Passwords come from environment variables, a prompt, or the OS keyring. They are never stored in the inventory file in plaintext.
* SSH runs on port 2222 as root and supports RSA, ECDSA, and Ed25519 keys.
* Planned: a job that pushes a fleet public key to nodes that still only accept a password.

## Interfaces
* **CLI** (`aredn-fleet`): `discover`, `plan` (dry run), `upgrade`, `radio`, `status`.
* **Web UI**: a topology view, a node checklist with filters (model, firmware, hop count), a plan preview, and live job progress with a stop button.
* Every job is **dry run first**. It shows the wave plan and needs an explicit confirmation before it runs.

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
* [ ] First real upgrade on the bench node (4.26.1.0 → 4.26.10.0)
* [ ] Radio settings job and node-side revert agent (.ipk)
* [ ] Web UI
* [ ] Field test on a lab mesh (2–3 spare nodes) before any production use
