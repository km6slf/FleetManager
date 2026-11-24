# READ-ONLY recon for AREDN release-candidate / nightly firmware.
# Finds the setting names and code paths behind the new features so the Fleet
# Manager can support them. Changes nothing on the node.
# Secrets are removed ON THE NODE before printing: radio SSIDs/keys are deleted
# from the radio JSON, and setup/uci lines that look like secrets are dropped.
scrub() { grep -viE 'passw|secret|psk|key|wpa|hash|ssid'; }
sec() { echo; echo "===== $* ====="; }
sec identity;       cat /etc/mesh-release; cat /tmp/sysinfo/board_name; uptime
sec memory;         grep -E 'MemTotal|MemAvailable' /proc/meminfo; df -h /tmp | tail -1
sec radios;         ucode -e 'import * as r from "aredn.radios"; const c = r.getConfiguration(); for (let i = 0; i < length(c); i++) { if (c[i] && c[i].mode) { delete c[i].mode.key; delete c[i].mode.ssid; } } printf("%J", c);' 2>&1 | sed -E 's/"(key|ssid)": "[^"]*"/"\1": "<removed>"/g' | head -c 40000
sec setup-keys;     cat /etc/config.mesh/setup | scrub
sec aredn-uci;      uci show aredn 2>&1 | scrub
sec dual-mesh;      f=/app/main/status/e/radio-and-antenna.ut; grep -n 'substr(radio0_mode' $f || echo "restriction gone"
sec multi-channel;  grep -nE 'channels|_channel' /app/main/status/e/radio-and-antenna.ut | grep -vE '^\s*$' | head -40
sec node-setup-ssid; grep -nE 'ssid|channels' /usr/local/bin/node-setup | head -40
sec wireless-reboot; awk '/case "wireless"/,/default:/' /usr/local/bin/node-setup | head -40
sec internet;       grep -rniE 'bypass|internet|gateway|share' /app/main/status/e/*.ut 2>/dev/null | cut -c1-200 | head -40
sec lan-address;    grep -niE 'lan_ip|lan_mask|dmz' /app/main/status/e/network.ut 2>/dev/null | cut -c1-200 | head -30
sec tunnels;        ls /app/main/status/e/; grep -niE 'wireguard|tunnel' /app/main/status/e/tunnels.ut 2>/dev/null | cut -c1-160 | head -20
sec sysinfo-links;  uclient-fetch -q -T 10 -O - 'http://127.0.0.1/a/sysinfo?link_info=1' 2>&1 | head -c 4000
sec sysupgrade;     md5sum /usr/local/bin/aredn_sysupgrade; grep -n 'sysupgrade' /usr/local/bin/aredn_sysupgrade | tail -3
sec done
