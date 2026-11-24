"""On/off node settings, learned from each node's own web dialogs.

Nothing here is a built-in list of AREDN settings. Each node's dialog templates
(``/app/main/status/e/*.ut``) are read over SSH and parsed for switches:

* the label, short description and help text the node shows;
* the setting the switch reads, and how: ``!== "0"`` (on unless 0), ``=== "1"``
  (on only if 1) or ``=== "0"`` (inverted, like the WAN firewall switch);
* what the dialog's save handler writes when the switch changes.

Only switches whose save handler writes exactly one setting, as on/off, can be changed in
bulk. A handler that also changes other settings (the WAN firewall switch turns three
others off) is shown, but left to the node's own dialog.

Changes go through the same steps the node's dialog and its Commit button use:
``aredn.configuration.prepareChanges()``, set the value in ``/etc/config.mesh``,
``saveSettings()``, ``commitChanges()`` (which runs node-setup and restarts what it says).
Dialog files depend only on firmware, so parsed definitions are cached per firmware version.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..transport import NodeSession, detached

DIALOG_DIR = "/app/main/status/e"
# Option names are never read or written if they look like secrets.
SECRET_RE = re.compile(r"passw|secret|psk|key|wpa|hash|ssid", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
SECTION_RE = re.compile(r"^(@[A-Za-z0-9_]+\[\d+\]|[A-Za-z0-9_]+)$")

# Print every dialog that has a switch, with a marker line per file.
DIALOGS_CMD = (f"for f in $(grep -l '_R(\"switch\"' {DIALOG_DIR}/*.ut 2>/dev/null); do "
               "echo \"@@FILE $f\"; cat \"$f\"; done")


@dataclass
class Target:
    """A uci option in /etc/config.mesh (``config.section.option``)."""
    config: str
    section: str
    option: str

    @property
    def key(self) -> str:
        return f"{self.config}.{self.section}.{self.option}"


@dataclass
class Condition:
    """``const <var> = uciMesh.get(...) == "<value>"``: when true, the switch is disabled."""
    var: str
    target: Target
    equals: str
    negate: bool = False


@dataclass
class SwitchDef:
    name: str                      # the dialog's form field
    dialog: str                    # e.g. "network"
    label: str
    description: str = ""
    help: str = ""
    read: Target | None = None
    read_rule: str = "ne0"         # ne0: on unless "0"; eq1: on only if "1"; eq0: on only if "0"
    write: Target | None = None    # None: not a simple on/off write
    on_value: str = "1"
    off_value: str = "0"
    other_writes: list[str] = field(default_factory=list)   # extra settings the handler changes
    disabled_if: Condition | None = None
    legacy_index: int | None = None   # older firmware: row number on its Advanced Configuration page
    needs_reboot: bool = False        # the node's page says a change needs a reboot
    blocked_reason: str = ""          # Fleet Manager's own reason not to change it in bulk

    @property
    def bulk_ok(self) -> bool:
        return self.write is not None and not self.other_writes and not self.blocked_reason

    @property
    def key(self) -> str:
        """What identifies "the same setting" across firmware: the option it writes/reads."""
        t = self.write or self.read
        return t.key if t else f"{self.dialog}:{self.name}"

    def why_not_bulk(self) -> str:
        if self.blocked_reason:
            return self.blocked_reason
        own = self.read.key if self.read else None
        others = list(dict.fromkeys(k for k in self.other_writes if k != own))
        if others:
            names = ", ".join(k.rsplit(".", 1)[-1] for k in others)
            return f"the node's dialog also changes {names} when this switch changes"
        if self.write is None:
            return "the node's dialog doesn't save this as a simple on/off setting"
        return ""

    def is_on(self, raw: str | None) -> bool:
        v = "" if raw is None else str(raw)
        if self.read_rule == "eq1":
            return v == "1"
        if self.read_rule == "eq0":
            return v == "0"
        return v != "0"

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(bulk_ok=self.bulk_ok, key=self.key, why_not_bulk=self.why_not_bulk())
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "SwitchDef":
        def t(x):
            return Target(**x) if x else None
        c = d.get("disabled_if")
        return cls(name=d["name"], dialog=d["dialog"], label=d["label"],
                   description=d.get("description", ""), help=d.get("help", ""),
                   read=t(d.get("read")), read_rule=d.get("read_rule", "ne0"), write=t(d.get("write")),
                   on_value=d.get("on_value", "1"), off_value=d.get("off_value", "0"),
                   other_writes=list(d.get("other_writes") or []),
                   legacy_index=d.get("legacy_index"), needs_reboot=bool(d.get("needs_reboot")),
                   blocked_reason=d.get("blocked_reason", ""),
                   disabled_if=Condition(c["var"], Target(**c["target"]), c["equals"], c.get("negate", False))
                   if c else None)


# --------------------------------------------------------------------------- parsing

_STR = r'"([^"\\]*)"'
_SWITCH_RE = re.compile(r'_R\(\s*"switch"\s*,\s*\{(?P<body>.*?)\}\s*\)\s*\}\}', re.S)
_GET_RE = re.compile(r'uciMesh\.get\(\s*' + _STR + r'\s*,\s*' + _STR + r'\s*,\s*' + _STR + r'\s*\)\s*'
                     r'(?P<op>!==|===|!=|==)\s*' + _STR)
_LABEL_RE = re.compile(r'<div class="o">(.*?)</div>\s*(?:<div class="m">(.*?)</div>)?', re.S)
_HELP_RE = re.compile(r'\A\s*(?:</div>\s*)*\{\{_H\("((?:[^"\\]|\\.)*)"\)\}\}', re.S)
_COND_RE = re.compile(r'const\s+(\w+)\s*=\s*uciMesh\.get\(\s*' + _STR + r'\s*,\s*' + _STR + r'\s*,\s*'
                      + _STR + r'\s*\)\s*(==|===|!=|!==)\s*' + _STR + r'\s*;')
_SET_RE = re.compile(r'uciMesh\.set\(\s*' + _STR + r'\s*,\s*' + _STR + r'\s*,\s*' + _STR + r'\s*,\s*(?P<val>[^;]*?)\)\s*;')


def _text(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s.replace("&zwnj;", ""))
    return re.sub(r"\s+", " ", s).strip()


def _handler_block(src: str, name: str) -> str | None:
    """The body of ``if ("<name>" in request.args) { ... }`` (balanced braces)."""
    m = re.search(r'if\s*\(\s*"' + re.escape(name) + r'"\s+in\s+request\.args\s*\)\s*\{', src)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(src) and depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i - 1]


def _parse_handler(src: str, name: str) -> tuple[Target | None, str, str, list[str]]:
    """(write target, on value, off value, other settings written) from the save handler."""
    block = _handler_block(src, name)
    if block is None:
        return None, "1", "0", []
    sets = list(_SET_RE.finditer(block))
    simple = re.compile(r'request\.args\.' + re.escape(name) + r'\s*===\s*"on"\s*\?\s*"?(\w+)"?\s*:\s*"?(\w+)"?$')
    targets = []
    for s in sets:
        targets.append((Target(s.group(1), s.group(2), s.group(3)), s.group("val").strip()))
    others = [t.key for t, _ in targets]
    # Exactly one set, of the on/off form, and nothing else that changes state.
    stripped = _SET_RE.sub("", block)
    if len(targets) == 1 and (m := simple.match(targets[0][1])) and not re.search(r"\w+\s*\(", stripped):
        return targets[0][0], m.group(1), m.group(2), []
    if len(targets) >= 1:
        # Not simple: report what it writes so the GUI can say why.
        return None, "1", "0", others
    return None, "1", "0", []


def parse_dialog(src: str, dialog: str) -> list[SwitchDef]:
    """Every switch in one dialog template, as the node defines it."""
    conds = {m.group(1): (Target(m.group(2), m.group(3), m.group(4)), m.group(5), m.group(6))
             for m in _COND_RE.finditer(src)}
    out: list[SwitchDef] = []
    for m in _SWITCH_RE.finditer(src):
        body = m.group("body")
        nm = re.search(r'name\s*:\s*' + _STR, body)
        if not nm or not NAME_RE.match(nm.group(1)):
            continue
        name = nm.group(1)
        labels = list(_LABEL_RE.finditer(src, max(0, m.start() - 1500), m.start()))
        label = _text(labels[-1].group(1)) if labels else name
        desc = _text(labels[-1].group(2) or "") if labels else ""
        hm = _HELP_RE.search(src[m.end():m.end() + 1500].split("_R(", 1)[0])
        help_ = _text(hm.group(1).replace('\\"', '"')) if hm else ""
        sw = SwitchDef(name=name, dialog=dialog, label=label, description=desc, help=help_)
        vm = re.search(r'value\s*:\s*(.*?)(?:,\s*disabled\s*:|$)', body, re.S)
        if vm and (g := _GET_RE.search(vm.group(1))):
            sw.read = Target(g.group(1), g.group(2), g.group(3))
            op, val = g.group("op"), g.group(5)
            if op in ("!==", "!=") and val == "0":
                sw.read_rule = "ne0"
            elif op in ("===", "==") and val == "1":
                sw.read_rule = "eq1"
            elif op in ("===", "==") and val == "0":
                sw.read_rule = "eq0"
            else:
                sw.read = None
        dm = re.search(r'disabled\s*:\s*(!?)\s*(\w+)\s*$', body.strip())
        if dm and dm.group(2) in conds:
            t, op, val = conds[dm.group(2)]
            neg = (op in ("!=", "!==")) != bool(dm.group(1))
            sw.disabled_if = Condition(dm.group(2), t, val, neg)
        sw.write, sw.on_value, sw.off_value, sw.other_writes = _parse_handler(src, name)
        if sw.write and sw.other_writes:
            sw.write = None
        # A switch that reads one setting but writes another (inverted ones excepted) isn't simple.
        if sw.write and sw.read and sw.write.key != sw.read.key:
            sw.other_writes, sw.write = [sw.write.key], None
        for t in (sw.read, sw.write):
            if t and (SECRET_RE.search(t.option) or not NAME_RE.match(t.config)
                      or not SECTION_RE.match(t.section) or not NAME_RE.match(t.option)):
                sw.read = sw.write = None
        out.append(sw)
    return out


# Settings that could cut this tool off from the node it's changing.
LOCKOUT_RE = re.compile(r"ssh_access|dropbear", re.I)


def _safety(sw: SwitchDef) -> SwitchDef:
    t = sw.write or sw.read
    if t and LOCKOUT_RE.search(t.option):
        sw.blocked_reason = ("could stop Fleet Manager reaching the node over SSH; change it on "
                             "the node itself")
    return sw


def parse_dialogs(dump: str) -> list[SwitchDef]:
    """Parse DIALOGS_CMD output (``@@FILE <path>`` markers)."""
    out: list[SwitchDef] = []
    parts = re.split(r"^@@FILE (\S+)\n", dump, flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        dialog = parts[i].rsplit("/", 1)[-1].removesuffix(".ut")
        out += parse_dialog(parts[i + 1], dialog)
    return [_safety(s) for s in out]


# --------------------------------------------------------------------------- older firmware (Lua UI)
#
# Firmware before the ucode web UI (3.23 and earlier) has one Advanced Configuration page,
# /www/cgi-bin/advancedconfig, built from a "settings" table: key (uci option), type, desc,
# condition, callbacks, needreboot. Showing the page (a plain GET) lists only the settings
# whose condition holds on that node, with their current values; saving is a POST of
# button_save_<n> / newval_<n>, which runs the page's own callbacks. Fleet Manager reads and
# saves through that page, so the node applies changes exactly as its own Save button would.

LEGACY_PAGE = "/www/cgi-bin/advancedconfig"
LEGACY_RENDER_CMD = (f"cd /www/cgi-bin && REQUEST_METHOD=GET QUERY_STRING= "
                     f"SCRIPT_NAME=/cgi-bin/advancedconfig ./advancedconfig 2>/dev/null")
LEGACY_SOURCE_CMD = f"sed -n '/^local settings = {{/,/^}}/p' {LEGACY_PAGE}"
UI_KIND_CMD = (f"[ -d {DIALOG_DIR} ] && echo ucode; [ -x {LEGACY_PAGE} ] && echo lua; true")

_ROW_RE = re.compile(r"<tr><td[^>]*>(?P<desc>.*?)<small>(?P<key>[^<]+)</small></td>\s*<td[^>]*>(?P<val>.*?)</td>", re.S)
_BOX_RE = re.compile(r"<input type='checkbox' id='field_(\d+)' name='newval_\1' value='1'( checked)?")
_CAT_RE = re.compile(r"<td colspan=3><b>(.*?)</b></td>")


def parse_legacy_source(src: str) -> list[dict[str, Any]]:
    """The page's settings table, in order (row n of the page is entry n, 1-based)."""
    out = []
    for block in re.findall(r"\{(.*?)\n\s*\}", src, re.S):
        e: dict[str, Any] = {}
        for k in ("key", "type", "category", "default"):
            if m := re.search(rf'\b{k}\s*=\s*"([^"]*)"', block):
                e[k] = m.group(1)
        e["needreboot"] = bool(re.search(r"needreboot\s*=\s*true", block))
        e["has_condition"] = "condition" in block
        if "key" in e:
            out.append(e)
    return out


def parse_legacy_page(html_text: str, source: list[dict[str, Any]]) -> tuple[list[SwitchDef], dict[str, str | None]]:
    """On/off settings the node's page shows, as SwitchDefs, plus their current values."""
    switches: list[SwitchDef] = []
    values: dict[str, str | None] = {}
    for m in _ROW_RE.finditer(html_text):
        box = _BOX_RE.search(m.group("val"))
        if not box:
            continue                      # text fields and actions aren't on/off settings
        idx, key = int(box.group(1)), m.group("key").strip()
        src = source[idx - 1] if 0 < idx <= len(source) else {}
        if src.get("key") != key or src.get("type") != "boolean":
            continue                      # page and table disagree: don't guess
        parts = key.split(".", 2)
        if len(parts) != 3 or not SECTION_RE.match(parts[1]) or not parts[1].startswith("@"):
            continue                      # pseudo settings (actions such as "aredn.olsr.restart")
        if SECRET_RE.search(parts[2]) or re.search(r"dangerous|force", key, re.I):
            continue                      # never offered in bulk
        desc_html = m.group("desc")
        bold = re.search(r"<b>(.*?)</b>", desc_html, re.S)
        label = _text(bold.group(1)) if bold else _text(desc_html)
        tgt = Target(*parts)
        sw = _safety(SwitchDef(name=f"newval_{idx}", dialog="advancedconfig", label=label,
                               description=_text(desc_html), read=tgt, read_rule="eq1", write=tgt,
                               legacy_index=idx, needs_reboot=bool(src.get("needreboot"))))
        switches.append(sw)
        values[tgt.key] = "1" if box.group(2) else "0"
    return switches, values


def legacy_save_command(index: int, on: bool) -> str:
    """POST one setting to the node's own page, as its Save Setting button does."""
    body = f"button_save_{int(index)}=Save+Setting" + (f"&newval_{int(index)}=1" if on else "")
    return (f"cd /www/cgi-bin && printf '%s' '{body}' | REQUEST_METHOD=POST "
            f"CONTENT_TYPE=application/x-www-form-urlencoded CONTENT_LENGTH={len(body)} "
            f"SCRIPT_NAME=/cgi-bin/advancedconfig ./advancedconfig >/dev/null 2>&1; echo saved-$?")


# --------------------------------------------------------------------------- reading a node

@dataclass
class NodeSettings:
    firmware: str
    switches: list[SwitchDef]
    values: dict[str, str | None]          # target key -> raw value (None = unset)
    read_at: float = 0.0
    legacy: bool = False                   # older firmware: read and saved through its Lua page

    def state(self, sw: SwitchDef) -> dict[str, Any]:
        raw = self.values.get(sw.read.key) if sw.read else None
        disabled = None
        if sw.disabled_if:
            v = self.values.get(sw.disabled_if.target.key)
            hit = (v == sw.disabled_if.equals) != sw.disabled_if.negate
            if hit:
                disabled = f"turned off by {sw.disabled_if.target.key} on this node"
        return {"key": sw.key, "on": sw.is_on(raw) if sw.read else None, "raw": raw, "disabled": disabled}

    def to_json(self) -> dict[str, Any]:
        return {"firmware": self.firmware, "read_at": self.read_at, "values": self.values,
                "legacy": self.legacy, "switches": [s.to_json() for s in self.switches]}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "NodeSettings":
        return cls(d.get("firmware", ""), [SwitchDef.from_json(s) for s in d.get("switches", [])],
                   dict(d.get("values") or {}), d.get("read_at", 0.0), bool(d.get("legacy")))


def _targets(switches: list[SwitchDef]) -> list[Target]:
    seen: dict[str, Target] = {}
    for s in switches:
        for t in (s.read, s.write, s.disabled_if.target if s.disabled_if else None):
            if t:
                seen[t.key] = t
    return list(seen.values())


def values_command(targets: list[Target]) -> str:
    """Print ``key=value`` for each option (empty if unset). Names are already validated."""
    return "; ".join(f"echo \"{t.key}=$(uci -q -c /etc/config.mesh get {t.key})\"" for t in targets)


def parse_values(out: str) -> dict[str, str | None]:
    vals: dict[str, str | None] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip() or None
    return vals


_DEF_CACHE: dict[str, list[SwitchDef]] = {}


async def read_node_settings(session: NodeSession, cache: dict[str, list[SwitchDef]] | None = None,
                             clock: Callable[[], float] | None = None) -> NodeSettings:
    import time
    cache = _DEF_CACHE if cache is None else cache
    fw = (await session.run("cat /etc/mesh-release")).stdout.strip().splitlines()
    fw = fw[0] if fw else ""
    kind = (await session.run(UI_KIND_CMD)).stdout
    if "ucode" not in kind and "lua" in kind:
        # Older firmware: the page decides what applies on this node, so read it every time.
        src = parse_legacy_source((await session.run(LEGACY_SOURCE_CMD, timeout=30)).stdout)
        sw, vals = parse_legacy_page((await session.run(LEGACY_RENDER_CMD, timeout=60)).stdout, src)
        return NodeSettings(fw, sw, vals, (clock or time.time)(), legacy=True)
    switches = cache.get(fw) if fw else None
    if switches is None:
        switches = parse_dialogs((await session.run(DIALOGS_CMD, timeout=60)).stdout)
        if fw:
            cache[fw] = switches
    tg = _targets(switches)
    vals = parse_values((await session.run(values_command(tg))).stdout) if tg else {}
    return NodeSettings(fw, switches, vals, (clock or time.time)())


# --------------------------------------------------------------------------- planning a change

@dataclass
class SettingChange:
    key: str      # SwitchDef.key, e.g. "aredn.@wan[0].mesh_internet"
    on: bool


@dataclass
class SettingStep:
    key: str
    label: str
    before: bool | None
    after: bool | None
    status: str           # change | unchanged | missing | not-bulk | disabled
    detail: str = ""
    value: str | None = None   # raw value to write


def plan_settings(ns: NodeSettings, changes: list[SettingChange]) -> list[SettingStep]:
    by_key = {s.key: s for s in ns.switches}
    steps = []
    for c in changes:
        sw = by_key.get(c.key)
        if sw is None:
            steps.append(SettingStep(c.key, c.key, None, None, "missing",
                                     f"this node's firmware ({ns.firmware}) has no such switch"))
            continue
        st = ns.state(sw)
        if not sw.bulk_ok:
            steps.append(SettingStep(c.key, sw.label, st["on"], None, "not-bulk", sw.why_not_bulk()))
        elif st["disabled"]:
            steps.append(SettingStep(c.key, sw.label, st["on"], None, "disabled",
                                     f"the node's dialog disables this switch: {st['disabled']}"))
        elif st["on"] == c.on:
            steps.append(SettingStep(c.key, sw.label, st["on"], c.on, "unchanged"))
        else:
            # Write the value the dialog writes for this state. For inverted reads (eq0) the
            # handler maps on/off itself, so "on" in the dialog is always the handler's on value.
            steps.append(SettingStep(c.key, sw.label, st["on"], c.on, "change",
                                     "the node's page says this needs a reboot" if sw.needs_reboot else "",
                                     value=sw.on_value if c.on else sw.off_value))
    return steps


VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
SETTINGS_LOG = "/tmp/aredn-fleet-settings.log"


def build_apply_command(writes: list[tuple[Target, str]]) -> str:
    """ucode run on the node: the same calls the dialog's save handler and Commit button make."""
    lines = []
    configs = []
    for t, v in writes:
        if not (NAME_RE.match(t.config) and SECTION_RE.match(t.section) and NAME_RE.match(t.option)
                and VALUE_RE.match(v)) or SECRET_RE.search(t.option):
            raise ValueError(f"refusing unsafe setting {t.key}={v!r}")
        lines.append(f'u.set("{t.config}", "{t.section}", "{t.option}", "{v}");')
        if t.config not in configs:
            configs.append(t.config)
    commits = " ".join(f'u.commit("{c}");' for c in configs)
    ucode = ('import * as c from "aredn.configuration"; import * as uci from "uci"; '
             'c.prepareChanges(); const u = uci.cursor("/etc/config.mesh"); '
             f'{" ".join(lines)} {commits} c.saveSettings(); c.commitChanges(); print("applied\\n");')
    # Detached: commitChanges can restart networking, which may drop this SSH session.
    return detached(f"ucode -e '{ucode}'", SETTINGS_LOG)


# --------------------------------------------------------------------------- the job

@dataclass
class SettingsNodeResult:
    node: str
    status: str                  # would-change | changed | unchanged | failed | skipped
    detail: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    reboot_required: bool = False


async def apply_node_settings(node: str, changes: list[SettingChange], connect, *,
                              execute: bool, sleep=asyncio.sleep, wait_seconds: float = 90,
                              poll: float = 5, on_read: Callable[[str, NodeSettings], None] | None = None,
                              log: Callable[..., None] | None = None) -> SettingsNodeResult:
    log = log or (lambda *a, **k: None)
    try:
        s = await connect(node)
    except Exception as exc:  # noqa: BLE001
        return SettingsNodeResult(node, "failed", f"couldn't log in: {exc}")
    try:
        ns = await read_node_settings(s)
        if on_read:
            on_read(node, ns)
        steps = plan_settings(ns, changes)
        res = SettingsNodeResult(node, "unchanged", steps=[asdict(x) for x in steps])
        todo = [x for x in steps if x.status == "change"]
        if not todo:
            skipped = [x for x in steps if x.status in ("missing", "not-bulk", "disabled")]
            res.detail = "; ".join(f"{x.label}: {x.detail}" for x in skipped) or "already set"
            return res
        if not execute:
            res.status = "would-change"
            return res
        by_key = {sw.key: sw for sw in ns.switches}
        if ns.legacy:
            return await _apply_legacy(s, node, ns, todo, by_key, res, on_read, log)
        writes = [(by_key[x.key].write, x.value) for x in todo]
        log(node, "settings-apply", writes=[(t.key, v) for t, v in writes])
        await s.run(build_apply_command(writes), timeout=30)
    finally:
        try:
            await s.close()
        except Exception:  # noqa: BLE001
            pass

    # Read back. The node may restart networking, so retry the login for a while.
    want = {by_key[x.key].read.key if by_key[x.key].read else by_key[x.key].write.key:
            (by_key[x.key], x.after) for x in todo}
    deadline = wait_seconds
    last = ""
    while deadline > 0:
        await sleep(poll)
        deadline -= poll
        try:
            s = await connect(node)
        except Exception as exc:  # noqa: BLE001
            last = f"not reachable yet ({exc})"
            continue
        try:
            out = (await s.run(f"cat {SETTINGS_LOG} 2>/dev/null; echo @@; "
                               + values_command([t for t in _targets(list(by_key.values()))])
                               + "; echo @@R; ls /tmp/reboot-required 2>/dev/null | head -1")).stdout
        finally:
            await s.close()
        logtxt, _, rest = out.partition("@@\n")
        rest, _, reboot = rest.partition("@@R")
        if re.search(r"error|exception|not found", logtxt, re.I):
            res.status, res.detail = "failed", f"the node reported: {logtxt.strip()[:300]}"
            return res
        vals = parse_values(rest)
        ns2 = NodeSettings(ns.firmware, ns.switches, vals)
        if on_read:
            on_read(node, ns2)
        wrong = [sw.label for sw, after in want.values() if ns2.state(sw)["on"] != after]
        if "applied" in logtxt and not wrong:
            res.status = "changed"
            res.reboot_required = bool(reboot.strip())
            res.detail = "reboot needed to finish (the node says so)" if res.reboot_required else ""
            return res
        last = f"not applied yet: {', '.join(wrong)}" if wrong else "waiting for the node to finish"
    res.status, res.detail = "failed", f"couldn't confirm the change: {last}"
    return res


async def _apply_legacy(s, node, ns, todo, by_key, res, on_read, log) -> SettingsNodeResult:
    """Save each setting through the node's own page (its callbacks run), then read the page
    back. The page only flags a reboot when one is needed; that's reported, not done."""
    for x in todo:
        sw = by_key[x.key]
        log(node, "settings-apply-legacy", key=x.key, on=x.after, row=sw.legacy_index)
        out = (await s.run(legacy_save_command(sw.legacy_index, bool(x.after)), timeout=120)).stdout
        if "saved-0" not in out:
            res.status, res.detail = "failed", f"the node's page didn't save {sw.label}: {out.strip()[:200]}"
            return res
    src = parse_legacy_source((await s.run(LEGACY_SOURCE_CMD, timeout=30)).stdout)
    sw2, vals = parse_legacy_page((await s.run(LEGACY_RENDER_CMD, timeout=60)).stdout, src)
    ns2 = NodeSettings(ns.firmware, sw2, vals, legacy=True)
    if on_read:
        on_read(node, ns2)
    by2 = {w.key: w for w in sw2}
    wrong = [by_key[x.key].label for x in todo
             if x.key not in by2 or ns2.state(by2[x.key])["on"] != x.after]
    if wrong:
        res.status, res.detail = "failed", f"the node's page doesn't show the change for: {', '.join(wrong)}"
        return res
    reboot = (await s.run("[ -e /tmp/reboot-required ] && echo yes; true")).stdout.strip() == "yes"
    res.status, res.reboot_required = "changed", reboot
    res.detail = "reboot needed to finish (the node's page says so)" if reboot else ""
    return res


def summarize(nodes: dict[str, NodeSettings]) -> list[dict[str, Any]]:
    """One row per setting across nodes: labels as the nodes show them, and who has what."""
    rows: dict[str, dict[str, Any]] = {}
    for n, ns in sorted(nodes.items()):
        for pos, sw in enumerate(ns.switches):
            r = rows.setdefault(sw.key, {"key": sw.key, "pos": pos, "labels": [], "descriptions": [], "help": "",
                                         "dialogs": [], "on": [], "off": [], "disabled": [],
                                         "not_bulk": {}, "nodes": [], "reboot": []})
            if sw.label not in r["labels"]:
                r["labels"].append(sw.label)
            if sw.description and sw.description not in r["descriptions"]:
                r["descriptions"].append(sw.description)
            r["help"] = r["help"] or sw.help
            if sw.dialog not in r["dialogs"]:
                r["dialogs"].append(sw.dialog)
            st = ns.state(sw)
            r["nodes"].append(n)
            (r["on"] if st["on"] else r["off"]).append(n)
            if st["disabled"]:
                r["disabled"].append(n)
            if not sw.bulk_ok:
                r["not_bulk"][n] = sw.why_not_bulk()
            if sw.needs_reboot:
                r["reboot"].append(n)
    for r in rows.values():
        r["missing"] = sorted(set(nodes) - set(r["nodes"]))
    # The order the nodes' own screens show them in.
    return sorted(rows.values(), key=lambda r: (r["dialogs"][0], r["pos"]))


def dumps(ns: NodeSettings) -> str:
    return json.dumps(ns.to_json())
