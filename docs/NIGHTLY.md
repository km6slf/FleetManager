# Looking ahead: AREDN release candidate (RC)

Source: the AREDN team's "Release Candidate – Major changes and tests" document (Nov 2026). The RC process started with nightly 20261012-e3528941. Later nightlies replace the RC as fixes land; the document names 20261027-e39b7e04 as current. The stable build that comes out of it becomes 4.26.X.0. `tools/recon/recon-nightly.sh` (read-only, removes secrets on the node) confirms the real setting names once the bench node is on the RC.

## What the RC changes for **upgrading** (affects Fleet Manager first)

| RC note | Why it matters | Fleet Manager change |
|---|---|---|
| **Back up your configuration before upgrading to an RC build** | Recovery, and the developers may ask for it | **Done:** each node's config backup is saved to this computer before flashing (`config/backups/<node>/<node>-<time>-<from-version>.tgz`). |
| **Mesh Station → PtMP: the PtMP end must be on 4.26.7.0 or (better) the RC before the station is upgraded** | This reverses edge-first for PtMP: the AP (inner) must go *before* its stations (outer) | **Done:** planner puts "AP before its stations" when both are selected (stations just drop off briefly while the AP reboots). Block a station whose AP is older than 4.26.7.0 and not being upgraded in the same run. Needs radio inventory to know which nodes are AP and which are station. |
| **AC radios lose channels 48, 52, 56 and 64**; wider channels overlapping them are gone too (e.g. 60 @ 40 MHz) | A node on one of these channels would come up on a channel that no longer exists | **Done:** pre-upgrade check for RC targets flags any 5 GHz radio whose channel and width overlap 48/52/56/64, and block it until it's moved. |
| **IPQ40xx reboot freeze (in 4.26.7.0)**: a few MikroTik IPQ40xx units (e.g. some hAP ac3) never finish a reboot; the RC fixes it, but the reboot *into* the RC still needs one power cycle | The node won't come back by itself after flashing, so the rollout times out and stops | **Done:** the review screen warns for `ipq40xx` hardware upgrading *from* 4.26.7.0 ("may need a power cycle; have someone on site"). If such a node doesn't return, the failure message says so. |
| Channel lists reworked: no regulatory filtering (the hardware decides), 40/80/160 MHz back, 3 GHz list fixed | Saved radio details go stale after an upgrade | **Done:** radios are re-read automatically after a node upgrades, and mark radio details stale when a node's firmware differs from when they were read. |
| Tunnels lose their 172.x addresses; new tunnels can't be made between RC and older nodes; non-upgraded nodes may show RC tunnels white or twice | Mixed-version meshes look odd in sysinfo | Discovery already de-duplicates links. Tunnel links are never part of upgrade verification. Don't treat white/duplicate tunnels as failures. |
| XLinks lose their IP address (VLAN only) | Anything that pinged old XLink IPs breaks | Note only. |
| Babel resyncs after tunnel/XLink changes; Babel route readiness is more responsive | Neighbours may take a moment to reappear | Keep the 5-minute neighbour timeout for now, then measure on the bench. |
| Memory: sysinfo now shows Available RAM (not Free); 64 MB devices gain about 6–8 MB | More headroom for the image in `/tmp` | The RAM preflight already uses `MemAvailable`. |

## New features and what Fleet Manager can do with them

| RC feature | Plan |
|---|---|
| **Multiple radios can each be Mesh, Mesh Station, Mesh PtP, Mesh PtMP or LAN hotspot** (still only one WAN client) | Done: per-band settings and the per-node multi-mesh probe. Confirm the probe on the RC, and keep refusing a second WAN client. |
| **Mesh Stations can have several channels**; the UI shows "Searching" until one is chosen | Find the setting format with the recon script. Then offer a multi-select in Mesh Station mode, and after a switch accept any listed channel (and tolerate "Searching" for a while). |
| **WAN shared to DtD neighbours ("DtD to WAN"), on by default** | Bulk settings job: show it in the Nodes table and let admins turn it off fleet-wide. |
| **A node can exclude itself from routing internet-bound traffic** | Bulk settings job toggle. |
| **WAN Monitor** withdraws WAN access the node advertises when its WAN is down | Show gateway status in the Nodes table if sysinfo exposes it. |
| **WAN MTU can be changed** | Per node only ("test with care", per the RC notes). |
| **Firewall/NAT can be disabled**; **Babel can run on the LAN**; **turning off LAN DHCP stops LAN route advertisement** | Per node only, with warnings. Not bulk. |
| **LAN network address can be changed** (masked by LAN type) | Per node only, never on the node this computer connects through. |
| **Ethernet port configuration stored differently** (migrates on upgrade) | A reason for the pre-upgrade backup. No direct change. |
| **Traceroute through RC nodes shows node names** | Use traceroute for path checks later. |
| **Wifi Signal/Scan work on all radios and in PtP/PtMP** | Possible later: signal check after radio changes. |
| AREDNLink v0.0.1 retired | Nothing to do. |

## Confirmed on the bench (hAP ac lite, nightly 20261122-5e4131bf)
- **Upgrade path unchanged:** `aredn_sysupgrade` still backs up and calls `/sbin/sysupgrade -f <backup> -q`. 4.26.1.0 → 4.26.10.0 → nightly both went through Fleet Manager.
- **Channel list format changed.** `getConfiguration()` now returns one `channels` list; each entry has `number`, `label`, `frequency`, `widths`, and per-width `unlicensed` (`maxEIRP`, `DFS`, `NO_OUTDOOR`) and `amateur` (`maxPEP`) limits. `managedOOB` is gone. The dialog lists every channel whose `widths` include the current width, in every mesh mode, labelled `<label> [ham N dBm] [dfs, indoor, N dB]` (the unlicensed part only when `setup.globals.radio_unlicensed` is on). **Done:** `channel_table()` reads both formats and builds the same labels.
- **Removed channels confirmed:** 48, 52, 56 and 64 are gone; 60 now only allows 5/10/20 MHz.
- **Setting functions renamed:** `aredn.configuration` has `getSettingAsString/AsInt/AsList` and `setSettingAsString/AsList`; `setSetting` is gone. **Done:** the radio script uses `setSettingAsString || setSetting`, so it works on both.
- **Multi-channel Mesh Station:** list setting `radioN_channels` (sorted, unique), with `radioN_channel` = the first; the list is empty when there's only one channel. Setting a single channel clears the list (now done by Fleet Manager too). Entering a non-mesh-ad-hoc mode moves channels ≤ 0 to 1.
- **Wireless changes no longer always reboot.** In `node-setup`, a wireless change reboots only if the hardware says so; a channel change restarts `wireless` and `lqm`. The radio job already handles both.
- **Two mesh radios:** the dialog's restriction is gone.
- **Internet settings** (uci `aredn.@wan[0]`, applied by `node-setup`, no reboot):

  | GUI | Setting | Default |
  |---|---|---|
  | Allow mesh to use local WAN | `mesh_to_local_wan` | 0 |
  | DtD to WAN | `local_defaultroute` (by its position in the dialog; confirm on a second node) | 1 |
  | LAN to local WAN | `lan_dhcp_route` | 1 |
  | LAN to mesh WAN | `lan_to_remote_wan` | 0 |
  | Firewall | `passthrough` (inverted) | 0 |
  | Mesh Internet Router | `mesh_internet` | 1 |
  | WAN monitor addresses | `monitor1`, `monitor2` | 1.1.1.1, 8.8.8.8 |
- **sysinfo:** `node_details.mesh_gateway` = `mesh_to_local_wan`. **Done:** a "gateway" badge in the Nodes table.

## Still to confirm (needs more nodes)
- How 6 GHz shows on the OpenWrt One. Fleet Manager infers 6 GHz from frequencies at or above 5925 MHz if the node doesn't name the band.
- How a multi-channel Mesh Station reports its current channel ("Searching") in `iw` and sysinfo.
