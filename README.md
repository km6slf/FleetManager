# AREDN Fleet Manager

Safe, bulk updating of AREDN® mesh nodes: discover the mesh, choose nodes from a checklist, and roll out firmware upgrades and radio settings **from the edge inward**, verifying each wave before moving on.

> Status: early development. The planner and discovery work; upgrade and settings jobs are in progress. Do not point it at a production mesh yet.

## Quick start
```bash
pip install -e ".[dev]"
aredn-fleet discover -o mesh.json            # crawl from localnode
aredn-fleet plan mesh.json --root <your-node> --parallel 2
pytest
```

## Layout
```
src/aredn_fleet/   controller package (discovery, topology planner, credentials, CLI, web UI)
agent/             optional on-node helper (.ipk) for staged apply / auto-revert
docs/DESIGN.md     design and safety model
tests/
```

See [docs/DESIGN.md](docs/DESIGN.md) for how the rollout order is computed and why radio changes are handled differently from firmware.

AREDN® is a registered trademark of Amateur Radio Emergency Data Network, Inc. This project is not affiliated with or endorsed by AREDN.
