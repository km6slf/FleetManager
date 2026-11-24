import asyncio
import json
from pathlib import Path

from aredn_fleet.jobs.settings import (NodeSettings, SettingChange, apply_node_settings,
                                       build_apply_command, parse_dialog, parse_dialogs,
                                       plan_settings, read_node_settings, summarize)
from aredn_fleet.transport import CommandResult

SRC = (Path(__file__).parent / "fixtures" / "network-20261122.ut").read_text()


def defs():
    return {s.name: s for s in parse_dialog(SRC, "network")}


def test_switches_come_from_the_dialog_with_its_labels():
    d = defs()
    assert set(d) == {"mesh_to_local_wan", "local_defaultroute", "lan_dhcp_route", "lan_to_remote_wan",
                      "lan_dhcp_defaultroute", "firewall", "lan_radio", "mesh_internet"}
    dtd = d["local_defaultroute"]
    assert dtd.label == "DtD to WAN" and dtd.description == "Allow any DtD connected node to use local WAN."
    assert dtd.help.startswith("If we have a WAN connection")
    assert dtd.write.key == "aredn.@wan[0].local_defaultroute" and dtd.read_rule == "ne0" and dtd.bulk_ok
    assert d["mesh_internet"].label == "Mesh Internet Router" and d["mesh_internet"].bulk_ok
    assert d["lan_radio"].read.key == "aredn.@babel[0].lan_radio" and d["lan_radio"].read_rule == "eq1"


def test_firewall_switch_is_inverted_and_not_bulk():
    fw = defs()["firewall"]
    assert fw.read.key == "aredn.@wan[0].passthrough" and fw.read_rule == "eq0"
    assert not fw.bulk_ok and "aredn.@wan[0].mesh_to_local_wan" in fw.other_writes
    assert "mesh_to_local_wan" in fw.why_not_bulk() and "passthrough" not in fw.why_not_bulk()


def test_disabled_condition_is_read_from_the_dialog():
    c = defs()["mesh_to_local_wan"].disabled_if
    assert c.var == "wan_passthrough" and c.target.key == "aredn.@wan[0].passthrough" and c.equals == "1"
    assert defs()["mesh_internet"].disabled_if is None


def ns(values, fw="20261122-5e4131bf"):
    return NodeSettings(fw, parse_dialog(SRC, "network"), values)


def test_plan_uses_node_values_and_dialog_rules():
    n = ns({"aredn.@wan[0].mesh_internet": "1", "aredn.@wan[0].local_defaultroute": None,
            "aredn.@wan[0].passthrough": "0"})
    steps = {s.key: s for s in plan_settings(n, [
        SettingChange("aredn.@wan[0].mesh_internet", False),
        SettingChange("aredn.@wan[0].local_defaultroute", True),      # unset counts as on (!== "0")
        SettingChange("aredn.@wan[0].passthrough", True),            # firewall: not bulk
        SettingChange("aredn.@wan[0].no_such", True)])}
    assert steps["aredn.@wan[0].mesh_internet"].status == "change"
    assert steps["aredn.@wan[0].mesh_internet"].value == "0"
    assert steps["aredn.@wan[0].local_defaultroute"].status == "unchanged"
    assert steps["aredn.@wan[0].passthrough"].status == "not-bulk"
    assert steps["aredn.@wan[0].no_such"].status == "missing"


def test_switch_disabled_on_the_node_is_skipped():
    n = ns({"aredn.@wan[0].passthrough": "1", "aredn.@wan[0].mesh_to_local_wan": "0"})
    (s,) = plan_settings(n, [SettingChange("aredn.@wan[0].mesh_to_local_wan", True)])
    assert s.status == "disabled"


def test_apply_command_uses_the_dialogs_calls_and_refuses_odd_values():
    d = defs()
    cmd = build_apply_command([(d["mesh_internet"].write, "0")])
    assert 'u.set("aredn", "@wan[0]", "mesh_internet", "0");' in cmd
    assert "c.prepareChanges()" in cmd and "c.commitChanges()" in cmd and "trap '' HUP" in cmd
    import pytest
    with pytest.raises(ValueError):
        build_apply_command([(d["mesh_internet"].write, "0; reboot")])


def test_parse_dialogs_dump_and_summary():
    dump = f"@@FILE /app/main/status/e/network.ut\n{SRC}"
    sw = parse_dialogs(dump)
    assert len(sw) == 8 and all(s.dialog == "network" for s in sw)
    a = ns({"aredn.@wan[0].mesh_internet": "0"})
    b = NodeSettings("4.26.1.0", [s for s in sw if s.name != "local_defaultroute"], {})
    rows = {r["key"]: r for r in summarize({"a": a, "b": b})}
    mi = rows["aredn.@wan[0].mesh_internet"]
    assert mi["off"] == ["a"] and mi["on"] == ["b"] and mi["labels"] == ["Mesh Internet Router"]
    assert rows["aredn.@wan[0].local_defaultroute"]["missing"] == ["b"]
    assert [r["labels"][0] for r in summarize({"a": a})][:2] == ["Mesh to WAN", "DtD to WAN"]


class FakeNode:
    def __init__(self, values):
        self.values = dict(values)
        self.applied = False
        self.commands = []

    async def connect(self, n):
        node = self

        class S:
            async def run(self, cmd, timeout=60):
                node.commands.append(cmd)
                if cmd.startswith("cat /etc/mesh-release"):
                    return CommandResult(0, "20261122-5e4131bf\n", "")
                if cmd.startswith("for f in"):
                    return CommandResult(0, f"@@FILE /app/main/status/e/network.ut\n{SRC}", "")
                if "ucode -e" in cmd:
                    import re
                    for k, v in re.findall(r'u\.set\("aredn", "(@\w+\[0\])", "(\w+)", "(\w+)"\)', cmd) and \
                            [(f"aredn.{a}.{b}", c) for a, b, c in
                             re.findall(r'u\.set\("aredn", "(@\w+\[0\])", "(\w+)", "(\w+)"\)', cmd)]:
                        node.values[k] = v
                    node.applied = True
                    return CommandResult(0, "", "")
                out = []
                if cmd.startswith("cat /tmp/aredn-fleet-settings.log"):
                    out.append("applied" if node.applied else "")
                    out.append("@@")
                import re
                for k in re.findall(r'echo "([^=]+)=', cmd):
                    out.append(f"{k}={node.values.get(k) or ''}")
                if "@@R" in cmd:
                    out.append("@@R")
                return CommandResult(0, "\n".join(out) + "\n", "")

            async def close(self):
                pass
        return S()


async def no_sleep(_):
    pass


def test_apply_changes_and_reads_back():
    node = FakeNode({"aredn.@wan[0].mesh_internet": "1"})
    ch = [SettingChange("aredn.@wan[0].mesh_internet", False)]
    dry = asyncio.run(apply_node_settings("a", ch, node.connect, execute=False, sleep=no_sleep))
    assert dry.status == "would-change" and not node.applied
    res = asyncio.run(apply_node_settings("a", ch, node.connect, execute=True, sleep=no_sleep))
    assert res.status == "changed", res.detail
    assert node.values["aredn.@wan[0].mesh_internet"] == "0"
    again = asyncio.run(apply_node_settings("a", ch, node.connect, execute=True, sleep=no_sleep))
    assert again.status == "unchanged"


def test_definitions_cached_per_firmware():
    node = FakeNode({})
    cache = {}

    async def go():
        s = await node.connect("a")
        await read_node_settings(s, cache)
        await read_node_settings(s, cache)
    asyncio.run(go())
    assert sum(c.startswith("for f in") for c in node.commands) == 1
    assert json.dumps(ns({}).to_json())
