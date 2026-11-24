"""Reading radios from older (Lua UI, 3.23) firmware: runs the real Lua with stand-ins."""
import json
import re

import pytest

from aredn_fleet.jobs.radio import LEGACY_RADIO_CMD, parse_radio_inventory, describe

FREQLIST = """\
* 2.412 GHz (Channel 1)
  2.417 GHz (Channel 2)
  2.462 GHz (Channel 11)
  2.467 GHz (Channel 12) [restricted]
  2.397 GHz (Channel -1)
"""

SETUP = """wifi_enable = 1
wifi_channel = -1
wifi_chanbw = 10
wifi_ssid = AREDN
wifi2_enable = 0
wifi2_key = 616263
wifi3_enable = 0
wifi3_key = 7365637265
"""


def run_lua(setup: str, phys: int) -> str:
    lupa = pytest.importorskip("lupa")
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    code = re.search(r"lua -e '(.*)'", LEGACY_RADIO_CMD, re.S).group(1)
    out = []
    g = lua.globals()
    g.py_setup, g.py_freq, g.py_phys = setup, FREQLIST, phys
    g.py_print = lambda s: out.append(s)
    g.py_json = lambda t: json.dumps(_to_py(t))
    lua.execute(r'''
      local function lines_of(s) return s:gmatch("[^\n]+") end
      local function fake(s) return { lines = function() return lines_of(s) end, close = function() end } end
      io.open = function(p) if p == "/etc/config.mesh/_setup" then return fake(py_setup) end end
      io.popen = function(c)
        if c:match("^ls /sys/class/ieee80211") then
          local t = {} for i = 0, py_phys - 1 do t[#t + 1] = "phy" .. i end
          return fake(table.concat(t, "\n"))
        end
        if c:match("freqlist") then return fake(py_freq) end
      end
      print = py_print
      package.preload["luci.jsonc"] = function() return { stringify = py_json } end
      package.preload["aredn.hardware"] = function()
        local hw = {}
        function hw.get_iface_name() return "wlan0" end
        function hw.get_rfbandwidths() return { 5, 10, 20 } end
        function hw.get_rfchannels(i)   -- 3.23's own parser
          local ch = {}
          for line in io.popen("iwinfo " .. i .. " freqlist"):lines() do
            local f, n = line:match("(%d+%.%d+) GHz %(Channel (%-?%d+)%)")
            if f and not line:match("restricted") then
              f = tonumber(("" .. f):gsub("%.", "") .. "")
              n = tonumber(n)
              ch[#ch + 1] = { label = n .. " (" .. f .. ")", number = n, frequency = f }
            end
          end
          return ch
        end
        return hw
      end
    ''')
    lua.execute(code)
    return out[0]


def _to_py(t):
    if hasattr(t, "items"):
        d = dict(t.items())
        if d and all(isinstance(k, int) for k in d):
            return [_to_py(d[k]) for k in sorted(d)]
        return {str(k): _to_py(v) for k, v in d.items()}
    return t


def test_reads_mesh_radio_like_basic_setup():
    raw = run_lua(SETUP, 1)
    assert "616263" not in raw and "7365637265" not in raw   # keys never leave the node
    inv = parse_radio_inventory(raw, "single-mesh")
    (r,) = inv.radios
    assert (r.band, r.mode, r.channel, r.bandwidth, r.ssid) == ("2.4GHz", "mesh", -1, 10, "AREDN")
    assert r.bws == [5, 10, 20]
    assert [c["number"] for c in r.channels["10"]] == [-1, 1, 2, 11]   # restricted 12 left out
    assert "Mesh" in describe(inv)


def test_mesh_off_single_radio_is_lan_hotspot():
    inv = parse_radio_inventory(run_lua(SETUP.replace("wifi_enable = 1", "wifi_enable = 0")
                                        .replace("wifi2_enable = 0", "wifi2_enable = 1"), 1))
    assert inv.radios[0].mode == "lan" and inv.radios[0].ssid is None


def test_second_radio_reports_purpose_only():
    inv = parse_radio_inventory(run_lua(SETUP.replace("wifi3_enable = 0", "wifi3_enable = 1"), 2))
    assert [r.mode for r in inv.radios] == ["mesh", "wan"]
    assert inv.radios[1].channels == {}
