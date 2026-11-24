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

## Rollout planning (firmware)
* `root` is the node the manager reaches the mesh through.
* `dependents(v)` is the set of nodes that become unreachable from root while v is down. It is computed by removing v and running a BFS.
* A node becomes **ready** once all of its *selected* dependents are done. Ready nodes are ordered by hop distance, farthest first. Root always goes last.
* **Waves** (bounded by `max_parallel`) admit a node only if both of these hold:
  * taking the wave down together cuts nothing beyond what each member's own dependents already account for (this catches joint cuts);
  * every member stays reachable while the rest of the wave is down, so each one can be verified right away.
* Warnings are raised when a selected node is the only path to an unselected node. Unreachable selected nodes are skipped and reported.

### Per-node upgrade steps
1. Pre-flight: check the credential, free RAM and /tmp space, and that the image matches the board ID, using the AREDN Firmware Selector metadata (sha256).
2. Copy the image to `/tmp` (scp -P 2222).
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
* [x] Credential store skeleton
* [x] CLI: `discover`, `plan`
* [ ] SSH transport (asyncssh), credential probing
* [ ] Firmware job and health checks
* [ ] Radio settings job and node-side revert agent (.ipk)
* [ ] Web UI
* [ ] Field test on a lab mesh (2–3 spare nodes) before any production use
