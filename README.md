# AREDN Fleet Manager

Safe, bulk updating of AREDN® mesh nodes: discover the mesh, choose nodes from a checklist, and roll out firmware upgrades and radio settings **from the edge inward**, verifying each wave before moving on.

> Status: early development, tested against a bench node. Don't point it at a production mesh yet.

![Nodes view](docs/screenshot-nodes.png)

## Desktop app (Windows)
Download from the [Releases](https://github.com/km6slf/FleetManager/releases) page. Every push to `main` also builds both versions, which you can download from that run's **Artifacts** under the Actions tab:
- **`AREDN-Fleet-Manager-<version>-setup.exe`** installs for your user, no admin rights needed, and adds a Start-menu shortcut.
- **`AREDN-Fleet-Manager-<version>-portable-win64.zip`** needs no install. Unzip it anywhere, even a USB stick, and run `AREDN-Fleet-Manager.exe`. Settings, the login list and downloaded firmware stay in a `data` folder next to it.

Both need the Microsoft Edge WebView2 runtime, which is built into Windows 10 and 11.

What's in it:
- **Nodes:** discover the mesh, filter it, and tick the nodes to work on. A column shows each node that is the *only path* to others.
- **Firmware upgrade:** preview the order (outermost first, your own node last), do a dry run, then **Upgrade…** with a typed confirmation. A run stops at the first failure.
- **Radio settings, per radio:** each band the selected nodes have (900 MHz, 2.4, 3.4, 5 or 6 GHz) gets its own section, with every node's current settings. Purpose, width and channel are chosen from lists that match the node's own Radios & Antennas screen, narrowed to what every selected node supports. Each RF cluster switches together and reverts on its own unless every node confirms. The **mesh SSID** defaults to `AREDN` (shown as e.g. `AREDN-10-v3`) and can be changed, for groups such as IDEC that use their own.
- **Node-specific settings:** give any node its own channel, width, purpose or SSID (with a note) while it stays in the group. Its values replace the group's when it's included in a change. These nodes carry a purple **custom** marker in the list.
- **Review before anything changes:** **Upgrade…** and **Switch…** check every selected node first, then show a ledger. For upgrades it lists each node by wave, in order, with current → new firmware. For radio changes it lists each node and radio with before → after for purpose, channel, width and SSID. It also flags blocked nodes, nodes with their own settings and links that will drop. Only the plan you reviewed can run.
- **Map:** nodes arranged by hops from you, with RF, DTD and tunnel links. Click to select.
- **Firmware:** download the 2 newest stable releases plus the nightly, for the hardware in your mesh, all hardware, or named boards. Every image is SHA-256 checked.
- **Credentials:** add SSH keys (by file path) and node passwords. Passwords go to Windows Credential Manager.
- **Activity:** live logs for every job, with a Stop button. A rollout stops between waves and never in the middle of a flash.

## Linux
A `.deb` is planned for both desktop use and headless use, such as a Pi on the mesh you reach from any browser. See [packaging/linux/README.md](packaging/linux/README.md). Until then:
```bash
pip install "aredn-fleet[serve] @ https://github.com/km6slf/FleetManager/archive/refs/heads/main.zip"
aredn-fleet serve --host 0.0.0.0     # prints a link with an access token
```

## Command line
```bash
pip install -e ".[dev,gui]"
aredn-fleet gui                              # the desktop app from source
aredn-fleet firmware channels                # 2 newest stable + newest nightly
aredn-fleet firmware fetch                   # sysupgrade images for all hardware, sha256-verified
aredn-fleet firmware fetch --board mikrotik,routerboard-952ui-5ac2nd   # just one hardware type
aredn-fleet creds add fleetkey --key ~/.ssh/aredn_ed25519
aredn-fleet creds add oldpw --password-env AREDN_PW_OLD
aredn-fleet discover -o mesh.json            # crawl from localnode
aredn-fleet plan mesh.json --root <your-node> --parallel 2
aredn-fleet upgrade mesh.json --root <your-node> --version 4.26.10.0           # dry run
aredn-fleet upgrade mesh.json --root <your-node> --version 4.26.10.0 --execute
aredn-fleet radio mesh.json --channel 133 --width 10                    # dry run; --execute to switch
pytest
```

## Layout
```
src/aredn_fleet/   controller package (discovery, topology planner, firmware, jobs, CLI)
src/aredn_fleet/web/   GUI: FastAPI app + static HTML/JS/CSS (no build step)
packaging/windows/ PyInstaller spec, Inno Setup installer, portable notes, icon
packaging/linux/   .deb plan
agent/             optional on-node helper (.ipk) for staged apply / auto-revert
docs/DESIGN.md     design and safety model
tests/
```

See [docs/DESIGN.md](docs/DESIGN.md) for how the rollout order is computed and why radio changes are handled differently from firmware.

AREDN® is a registered trademark of Amateur Radio Emergency Data Network, Inc. This project is not affiliated with or endorsed by AREDN.
