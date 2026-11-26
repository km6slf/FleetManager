# Changelog

All notable changes to AREDN Fleet Manager. Newest first.

Every build shows its exact build number at the top right of the window (for example `v0.1.0-dev.aa17f60`) and on the first line of every job log. Builds from `main` are published as the **Development build** on GitHub and as the `dev` Docker image; releases get their own version and the `latest` Docker image.

## Unreleased (development build)

### Fixed
- **Discovery now finds the whole mesh.** It used to find nodes only by following each node's list of direct neighbours, which misses nodes reached over a tunnel or over a link the node doesn't name. In a field test, Fleet Manager showed 4 nodes while localnode's mesh page showed 10. Discovery now also reads the start node's own node list (the same list as its mesh status page; LAN devices such as cameras aren't in it) and reads every node the neighbour walk missed. Their own neighbour lists usually connect them into the map. The Discover log names the nodes found this way.
- A node that answers but that none of the nodes read links to is marked **no known path** (not "unreachable"). You can read and change its settings, but upgrades skip it, because a safe edge-inward order can't be worked out without its links.

- **Upgrades on low-memory nodes** (32 MB class, e.g. hAP lite). The memory check used to require the image plus a 4 MB margin of my own choosing, measured without preparation, so a hAP lite with 10.9 MB free was refused (and its free memory drifted across the limit between the dry run and the real run). Now Fleet Manager does what the node's own Firmware page does before an upload: drops the page cache and lowers the kernel's free-memory reserve to 512 kB (put back if the upgrade doesn't start); the margin is 2 MB. If the node is still short, it frees memory **without asking**: it stops the node's `manager` service (AREDN's own `aredn_sysupgrade` stops it just before flashing anyway, and a clean stop switches its hardware watchdog off properly), ends any iperf test and deletes the Firmware page's leftover downloads, then measures again. The dry-run review says when this will happen. If there still isn't enough, the service is started again and the node is left unchanged. The routing, web, SSH, DNS and telnet daemons are never stopped early: AREDN's hardware watchdog reboots a node when one of them disappears.
- **Older (3.x) nodes:** the upgrade script never actually stopped the services listed in the node's `upgrade_prepare.sh` before flashing (a Python escape turned the `sed` back-reference into a control character). It now stops them, except `dropbear`, as the node's own Firmware page does.

- **Radio changes no longer need correct clocks on the nodes.** The job used to refuse any node whose clock was more than 20 seconds off, because every node switches at a shared time; nodes off the Internet often lose their time (in the field, several days). Fleet Manager now measures each node's clock when arming it and gives it the switch time in its own clock, so they still switch at the same moment (to within a second). A wrong clock shows as a note in the review instead of blocking.

### Added
- **Remove nodes from the review screen.** Each node in the upgrade, radio and settings reviews has a **Remove** button, and the top has **Remove blocked** and **Remove already current** (or **unchanged**). Removing deselects the node and checks the plan again, since a blocked node locks the Go button and Go only runs the plan you reviewed. The reason a node is blocked now sits under its status instead of in a column cut off at the right.
- The exact build number in the window, the Docker image and the first line of every job log, so you can tell which build you're running and which build produced a log.
- This changelog.
- **Docker version** (Linux amd64/arm64 and Windows containers) published as `ghcr.io/km6slf/fleetmanager`, with Compose file, example settings and install/upgrade instructions in the README. Saved passwords go in an encrypted file in the data volume; the access link stays the same across restarts and upgrades.
- Every push to `main` is published as the rolling **Development build** pre-release (installer and portable zip).
- README: goals, roadmap, known issues and limitations.

## 0.1.0-alpha.1 (2026-11-24)

First release for outside testing.

- Mesh discovery and map, edge-inward firmware upgrades with dry run, review ledger and typed confirmation; config backup before every flash.
- Several passwords and SSH keys per fleet; SSH host keys pinned.
- Radio settings per band for 1- and 2-radio nodes (purpose, channel, width, SSID), switched together per RF cluster with automatic revert; LAN Hotspot and WAN Client; per-node settings.
- Node settings (on/off switches) learned from each node's own pages.
- Release-candidate safeguards (PtMP access points before stations, removed AC channels, IPQ40xx warning).
- Older OLSR-era nodes: read status, radios and settings; upgrade with the older procedure; OLSR → Babel migration checks; last release per hardware.
- Mesh names looked up on the mesh (works with Wi-Fi on); retries on lossy links; leftover-route warning on Windows.
- New device wizard (guided first install).
- Windows installer and portable zip.
