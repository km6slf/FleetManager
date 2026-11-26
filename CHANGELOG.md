# Changelog

All notable changes to AREDN Fleet Manager. Newest first.

Every build shows its exact build number at the top right of the window (for example `v0.1.0-dev.aa17f60`) and on the first line of every job log. Builds from `main` are published as the **Development build** on GitHub and as the `dev` Docker image; releases get their own version and the `latest` Docker image.

## Unreleased (development build)

### Fixed
- **Discovery now finds the whole mesh.** It used to find nodes only by following each node's list of direct neighbours, which misses nodes reached over a tunnel or over a link the node doesn't name. In a field test, Fleet Manager showed 4 nodes while localnode's mesh page showed 10. Discovery now also reads the start node's own node list (the same list as its mesh status page; LAN devices such as cameras aren't in it) and reads every node the neighbour walk missed. Their own neighbour lists usually connect them into the map. The Discover log names the nodes found this way.
- A node that answers but that none of the nodes read links to is marked **no known path** (not "unreachable"). You can read and change its settings, but upgrades skip it, because a safe edge-inward order can't be worked out without its links.

### Added
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
