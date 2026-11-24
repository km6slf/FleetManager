# Linux .deb — plan

Status: planned. The Windows build ships first; this is the design for the Linux package.

## Two ways to run it on Linux
1. **Desktop app.** `aredn-fleet-gui` opens the same UI in a native window. pywebview uses
   WebKitGTK on Linux, so it needs the distro's GTK bindings.
2. **Headless service.** This is for a Pi or VM that sits on the mesh. A systemd unit runs
   `aredn-fleet serve --host 0.0.0.0 --port 8787`, and you open the UI from any browser on
   the mesh. Most clubs will want this mode, because the manager should live on the mesh
   rather than on someone's laptop.

## Package layout
```
/opt/aredn-fleet/venv/            Python venv with aredn-fleet[gui] and its pinned deps
                                  (created with --system-site-packages so it can use
                                  the distro's python3-gi for WebKitGTK)
/usr/bin/aredn-fleet              -> /opt/aredn-fleet/venv/bin/aredn-fleet
/usr/bin/aredn-fleet-gui          -> /opt/aredn-fleet/venv/bin/aredn-fleet-gui
/usr/share/applications/aredn-fleet.desktop
/usr/share/icons/hicolor/512x512/apps/aredn-fleet.png
/lib/systemd/system/aredn-fleet.service    (installed disabled)
/var/lib/aredn-fleet/             service state (user: aredn-fleet)
```

**Why a venv rather than PyInstaller on Linux?** A PyInstaller bundle has to carry its own
copy of the GTK/WebKit introspection bindings, and those break when the distro's system
libraries differ. A test build here hit exactly that. A venv that reuses the system
`python3-gi` avoids the problem.

**Why a venv rather than all-distro packages?** fastapi, uvicorn, asyncssh and pywebview
aren't packaged at the same versions across Debian and Ubuntu releases.

## Depends
`python3 (>= 3.10)`, `python3-venv`, `python3-gi`, `gir1.2-webkit2-4.1`, `libsecret-1-0`.
The service mode alone doesn't need GTK, so those three can become `Recommends`.

## Targets
Build one .deb per distro release, because the venv is tied to that release's Python minor version:
- Ubuntu 24.04 LTS on amd64 and arm64
- Debian 12 / Raspberry Pi OS (bookworm) on arm64 and armhf

## Build (CI)
A GitHub Actions matrix builds each target in its distro container:
1. Install the build deps.
2. Create the venv in `/opt/aredn-fleet`.
3. Run `pip install .[gui]`.
4. Stage the tree and run `dpkg-deb --build`.
5. In a fresh container, `apt install ./aredn-fleet_*.deb`, then run
   `aredn-fleet-gui --selftest` (headless) and `aredn-fleet --help`.
6. Attach the .debs to the same GitHub Release as the Windows installer.

## Service mode details still to build
- **Persistent token:** `aredn-fleet serve --token-file /var/lib/aredn-fleet/token`, so the
  access link survives restarts. `aredn-fleet show-url` prints it for an admin.
- **Dedicated system user** `aredn-fleet`. Its SSH keys live in
  `/var/lib/aredn-fleet/.ssh`, and node passwords go to an encrypted file
  (keyring's file backend), because a headless box has no desktop keyring.
- **HTTPS (optional):** for service mode, a self-signed certificate. The mesh itself is
  plain HTTP, so this is opt-in.
