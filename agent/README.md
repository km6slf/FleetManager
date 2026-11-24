# aredn-fleet-agent (optional .ipk)

A small on-node helper for the Fleet Manager. It is not required for discovery or firmware upgrades.

Planned commands:
- `fleet-agent status` returns versioned JSON (firmware, radio config, neighbors).
- `fleet-agent stage <file>` stores pending radio settings without applying them.
- `fleet-agent apply --at <epoch> --revert-after <sec>` applies the staged settings at a set time and rolls them back automatically unless confirmed.
- `fleet-agent confirm` keeps the new settings and disarms the revert timer.

The dead-man revert has to run on the node itself, because a node can lose contact with the manager partway through a channel or mode change.
