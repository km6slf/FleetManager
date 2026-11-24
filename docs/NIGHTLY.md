# Looking ahead: AREDN release-candidate features

Source: the release-candidate slides from Pacificon 2026. The full release notes are a private Google Doc, and access has been requested. The bench node gets these features by upgrading to the latest nightly. Then `tools/recon/recon-nightly.sh` (read-only, and it removes secrets on the node) shows the real setting names.

| RC feature | What it means for Fleet Manager | Plan |
|---|---|---|
| **Multi-radio mesh.** All radios can mesh at once, e.g. 2.4 + 5 GHz bridging a regional mesh to a local one | Today the node's own UI turns the other radio off when you pick a second mesh radio. Fleet Manager refuses that combination unless the node supports it. | **Done:** per-band radio settings. Each node is probed for the restriction in its own UI code (`multi_mesh`). Still to do: confirm the probe on the nightly. |
| **Multi-channel Mesh Stations.** A station is given several channels and picks the best PtMP to connect to (SSID becomes `AREDN-X-<bw>-v3`) | The channel becomes a *list*, for Mesh Station mode only | Find the setting format on the nightly (`radioN_channel` as a list, or a new key). Then show a multi-select in Mesh Station mode when every selected node supports it, and check after the switch that the node joined one of the listed channels. |
| **Internet bypass.** A node can opt out of carrying internet-bound traffic | A per-node routing setting. It doesn't touch RF, so there's no cluster switch | Add a **bulk settings** job with the same backup, revert and confirm machinery (no shared switch time). Find the key on the nightly. |
| **A dead internet connection is no longer advertised** | Automatic behaviour | Show each node's gateway state in the Nodes table if sysinfo exposes it. |
| **Internet attached to one node can be shared with local nodes** | Per-node setting | Same bulk settings job. |
| **The LAN network address can be changed** | Changes the node's IP addressing. If it's the node this computer reaches the mesh through, the manager loses its connection. | Never bulk. Per node at most, with an explicit warning, and never on the root node. |
| **Tunnels no longer use IP addresses** | Link types reported by sysinfo may change | Check `linkType` values on the nightly so tunnels are still recognised (the "Don't count on tunnel links" option depends on it). |
| **Babel rerouting is more responsive** | Routes recover faster after a reboot | Measure on the bench, then shorten the default soak time and neighbour timeouts. |
| **64 MB devices have about 8 MB more free memory** | More headroom for the firmware image in `/tmp` | Re-measure; the RAM preflight check stays. |
| **Mesh page tool shortcuts** | None | None |

## Also to check on the nightly
- Whether a wireless change still forces a reboot. On 4.26.1.0, `node-setup`'s `wireless` case falls through to `default`. If the nightly fixes this, radio changes become a quick service restart instead of a reboot. The revert script already handles both.
- Whether `aredn_sysupgrade` changed.
- The new `/a/sysinfo` fields, such as per-radio info. If sysinfo reports every radio, the Nodes table can show radios without logging in.
- Whether 6 GHz on the OpenWrt One appears as a separate radio. Fleet Manager infers 6 GHz from frequencies at or above 5925 MHz if the node doesn't name the band.
