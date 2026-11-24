-- 'settings' table from /www/cgi-bin/advancedconfig, AREDN 3.23.8.0 (GPL-3.0, AREDN contributors)
local settings = {
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].enable",
        type = "boolean",
        desc = "Enable <b>Link Quality Management</b><br><br><small>aredn.@lqm[0].enable</small>",
        default = "1",
        postcallback = "lqm_defaults()",
        needreboot = true
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].margin_snr",
        type = "string",
        desc = "<b>SNR Margin</b> in dB above Min SNR a signal must reach to be re-activated<br><br><small>aredn.@lqm[0].margin_snr</small>",
        default = "1",
        condition = "lqm_enabled()"
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].min_distance",
        type = "string",
        desc = "<b>Min Distance</b> in meters beyond which a neighbor RF link is allowed<br><br><small>aredn.@lqm[0].min_distance</small>",
        default = "0",
        condition = "lqm_enabled()"
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].auto_distance",
        type = "string",
        desc = "<b>Default Distance</b> in meters to use when actual distance cannot be calculated<br><br><small>aredn.@lqm[0].auto_distance</small>",
        default = "0",
        condition = "lqm_enabled()"
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].margin_quality",
        type = "string",
        desc = "<b>Quality Margin</b> percentage increase before neighbor can be re-activated<br><br><small>aredn.@lqm[0].margin_quality</small>",
        default = "1",
        condition = "lqm_enabled()"
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].ping_penalty",
        type = "string",
        desc = "<b>Ping Penalty</b> quality percentage to add when neighbor cannot be pinged<br><br><small>aredn.@lqm[0].ping_penalty</small>",
        default = "5",
        condition = "lqm_enabled()"
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].rts_threshold",
        type = "string",
        desc = "<b>RTS Threshold</b> in bytes before using RTS/CTS when hidden nodes are detected<br><br><small>aredn.@lqm[0].rts_threshold</small>",
        default = "1",
        condition = "lqm_enabled()"
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].mtu",
        type = "string",
        desc = "<b>Maximum packet size</b> in bytes sent over WiFi (256 to 1500)<br><br><small>aredn.@lqm[0].mtu</small>",
        default = "1500",
        postcallback = "changeMTU()",
        needreboot = true
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].user_blocks",
        type = "string",
        desc = "<b>User Blocked</b> comma-separated list of blocked MACs<br><br><small>aredn.@lqm[0].user_blocks</small>",
        default = "",
        condition = "lqm_enabled()"
    },
    {
        category = "Link Quality Settings",
        key = "aredn.@lqm[0].user_allows",
        type = "string",
        desc = "<b>User Allowed</b> comma-separated list of always allowed MACs<br><br><small>aredn.@lqm[0].user_allows</small>",
        default = "",
        condition = "lqm_enabled()"
    },
    {
        category = "WAN Settings",
        key = "aredn.@wan[0].olsrd_gw",
        type = "boolean",
        desc = "<b>Allow other MESH nodes to use my WAN</b> - not recommended and OFF by default<br><br><small>aredn.@wan[0].olsrd_gw</small>",
        default = "0",
        postcallback = "changeWANGW()",
        needreboot = true
    },
    {
        category = "WAN Settings",
        key = "aredn.@wan[0].lan_dhcp_route",
        type = "boolean",
        desc = "<b>Allow my LAN devices to access my WAN</b> - ON by default<br><br><small>aredn.@wan[0].lan_dhcp_route</small>",
        default = "1",
        postcallback = "changeWANGW()",
        needreboot = true
    },
    {
        category = "WAN Settings",
        key = "aredn.@wan[0].lan_dhcp_defaultroute",
        type = "boolean",
        desc = "<b>Provide default route to LAN devices</b> even when WAN access is disabled<br><br><small>aredn.@wan[0].lan_dhcp_defaultroute</small>",
        default = "0",
        postcallback = "changeWANGW()",
        needreboot = true
    },
    {
        category = "WAN Settings",
        key = "aredn.wan.vlanid",
        type = "string",
        desc = "<b>WAN VLAN Number</b> - must be an integer in the range [1,4094]<br><br><small>aredn.wan.vlanid</small>",
        default = "",
        condition = "supportsVLANChange()",
        current = "currentWANVLAN()",
        postcallback = "changeWANVLAN()",
        needreboot = true
    },
    {
        category = "WAN Settings",
        key = "aredn.@wan[0].web_access",
        type = "boolean",
        desc = "<b>Enable web access</b> to the node from the WAN interface<br><br><small>aredn.@wan[0].web_access</small>",
        default = "1",
        needreboot = true
    },
    {
        category = "WAN Settings",
        key = "aredn.@wan[0].ssh_access",
        type = "boolean",
        desc = "<b>Enable SSH access</b> to the node from the WAN interface<br><br><small>aredn.@wan[0].ssh_access</small>",
        default = "1",
        needreboot = true
    },
    {
        category = "WAN Settings",
        key = "aredn.@wan[0].telnet_access",
        type = "boolean",
        desc = "<b>Enable TELNET access</b> to the node from the WAN interface<br><br><small>aredn.@wan[0].telnet_access</small>",
        default = "1",
        needreboot = true
    },
    {
        category = "Power Options",
        key = "aredn.@poe[0].passthrough",
        type = "boolean",
        desc = "<b>PoE Passthrough</b> specifies whether PoE power should be enabled (Not all devices have PoE passthrough ports)<br><br><small>aredn.@poe[0].passthrough</small>",
        default = "0",
        condition = "hasPOE()",
        postcallback = "setPOEOutput()"
    },
    {
        category = "Power Options",
        key = "aredn.@usb[0].passthrough",
        type = "boolean",
        desc = "<b>USB Power Passthrough</b> specifies whether USB power should be enabled (Not all devices have USB powered ports)<br><br><small>aredn.@usb[0].passthrough</small>",
        default = "1",
        postcallback = "setUSBOutput()",
        condition = "hasUSB()"
    },
    {
        category = "Tunnel Options",
        key = "aredn.@tunnel[0].maxclients",
        type = "string",
        desc = "<b>Tunnel Maxclients</b> specifies the maximum number of tunnel clients this node can serve; must be an integer in the range [0,100].<br><br><small>aredn.@tunnel[0].maxclients</small>",
        default = "10",
        precallback = "restrictTunnelLimitToValidRange()",
        postcallback = "adjustTunnelInterfaceCount()"
    },
    {
        category = "Tunnel Options",
        key = "aredn.@tunnel[0].maxservers",
        type = "string",
        desc = "<b>Tunnel Maxservers</b> specifies the maximum number of tunnel servers to which this node can connect; must be an integer in the range [0,100].<br><br><small>aredn.@tunnel[0].maxservers</small>",
        default = "10",
        precallback = "restrictTunnelLimitToValidRange()",
        postcallback = "adjustTunnelInterfaceCount()"
    },
    {
        category = "Tunnel Options",
        key = "aredn.@tunnel[0].wanonly",
        type = "boolean",
        desc = "<b>WAN-Only Tunnel</b> prevents tunnel traffic from being routed over the Mesh network itself<br><br><small>aredn.@tunnel[0].wanonly</small>",
        default = "1",
        needreboot= true
    },
    {
        category = "Memory Settings",
        key = "aredn.@meshstatus[0].lowmem",
        type = "string",
        desc = "<b>Low Memory Threshold</b> in KB when the Mesh Status page will be truncated<br><br><small>aredn.@meshstatus[0].lowmem</small>",
        default = "10000"
    },
    {
        category = "Memory Settings",
        key = "aredn.@meshstatus[0].lowroutes",
        type = "string",
        desc = "<b>Low Memory Max Routes</b> is the maximum number of routes shown on the Mesh Status page when low memory is detected<br><br><small>aredn.@meshstatus[0].lowroutes</small>",
        default = "1000"
    },
    {
        category = "Network Tools",
        key = "aredn.olsr.restart",
        type = "none",
        desc = "<b>OLSR Restart</b> will restart OLSR when executed; wait up to 2 or 3 minutes to receive response<br><br><small>aredn.olsr.restart</small>",
        default = "0",
        postcallback = "olsr_restart()"
    },
    {
        category = "Network Tools",
        key = "aredn.@iperf[0].enable",
        type = "boolean",
        desc = "<b>IPERF Enable</b> allows the included iperf3 client/server<br><br><small>aredn.@iperf[0].enable</small>",
        default = "1"
    },
    {
        category = "Map Paths",
        key = "aredn.@map[0].maptiles",
        type = "string",
        desc = "<b>Map Tiles URL</b><br><br><small>aredn.@map[0].maptiles</small>",
        default = "http://stamen-tiles-{s}.a.ssl.fastly.net/terrain/{z}/{x}/{y}.jpg"
    },
    {
        category = "Map Paths",
        key = "aredn.@map[0].leafletcss",
        type = "string",
        desc = "<b>Leaflet.css URL</b><br><br><small>aredn.@map[0].leafletcss</small>",
        default = "http://unpkg.com/leaflet@0.7.7/dist/leaflet.css"
    },
    {
        category = "Map Paths",
        key = "aredn.@map[0].leafletjs",
        type = "string",
        desc = "<b>Leaflet.js URL</b><br><br><small>aredn.@map[0].leafletjs</small>",
        default = "http://unpkg.com/leaflet@0.7.7/dist/leaflet.js"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].firmwarepath",
        type = "string",
        desc = "<b>Firmware Download URL</b><br><br><small>aredn.@downloads[0].firmwarepath</small>",
        default = "http://downloads.arednmesh.org/firmware"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].pkgs_core",
        type = "string",
        desc = "<b>Core Packages Download URL</b><br><br><small>aredn.@downloads[0].pkgs_core</small>",
        default = defaultPackageRepos('aredn_core'),
        postcallback = "writePackageRepo('core')"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].pkgs_base",
        type = "string",
        desc = "<b>Base Packages URL</b><br><br><small>aredn.@downloads[0].pkgs_base</small>",
        default = defaultPackageRepos('base'),
        postcallback = "writePackageRepo('base')"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].pkgs_arednpackages",
        type = "string",
        desc = "<b>AREDN Packages URL</b><br><br><small>aredn.@downloads[0].pkgs_arednpackages</small>",
        default = defaultPackageRepos('arednpackages'),
        postcallback = "writePackageRepo('arednpackages')"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].pkgs_luci",
        type = "string",
        desc = "<b>Luci Packages URL</b><br><br><small>aredn.@downloads[0].pkgs_luci</small>",
        default = defaultPackageRepos('luci'),
        postcallback = "writePackageRepo('luci')"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].pkgs_packages",
        type = "string",
        desc = "<b>Package Download URL</b> for packages not included in the other sections<br><br><small>aredn.@downloads[0].pkgs_packages</small>",
        default = defaultPackageRepos('packages'),
        postcallback = "writePackageRepo('packages')"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].pkgs_routing",
        type = "string",
        desc = "<b>Routing Packages URL</b><br><br><small>aredn.@downloads[0].pkgs_routing</small>",
        default = defaultPackageRepos('routing'),
        postcallback = "writePackageRepo('routing')"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].pkgs_telephony",
        type = "string",
        desc = "<b>Telephony Packages URL</b><br><br><small>aredn.@downloads[0].pkgs_telephony</small>",
        default = defaultPackageRepos('telephony'),
        postcallback = "writePackageRepo('telephony')"
    },
    {
        category = "Firmware",
        key = "aredn.@downloads[0].pkgs_freifunk",
        type = "string",
        desc = "<b>Freifunk Packages URL</b><br><br><small>aredn.@downloads[0].pkgs_freifunk</small>",
        default = defaultPackageRepos('freifunk'),
        postcallback = "writePackageRepo('freifunk')"
    },
    {
        category = "Firmware",
        key = "aredn.firmware.dangerous_upgrade",
        type = "boolean",
        desc = "<b>Dangerous Upgrade Disables</b> all safety checks usually applied when upgrading firmware<br><br><small>aredn.firmware.dangerous_upgrade</small>",
        default = "0",
        current = "current_force_upgrade()",
        postcallback = "update_force_upgrade()"
    },
    {
        category = "AREDN Alert Settings",
        key = "aredn.aam.refresh",
        type = "none",
        desc = "<b>Alert Message Refresh</b> - Execute to pull any AREDN Alert messages<br><br><small>aredn.aam.refresh</small>",
        default = "0",
        postcallback = "aam_refresh()"
    },
    {
        category = "AREDN Alert Settings",
        key = "aredn.@alerts[0].localpath",
        type = "string",
        desc = "<b>Alert Message Local URL</b> - location from which local AREDN Alerts can be downloaded<br><br><small>aredn.@alerts[0].localpath</small>",
        default = ""
    },
    {
        category = "AREDN Alert Settings",
        key = "aredn.@alerts[0].groups",
        type = "string",
        desc = "<b>Alert Message Groups</b> - comma seperated list of group names to check for alert messages<br><br><small>aredn.@alerts[0].groups</small>",
        default = ""
    },
    {
        category = "AREDN Alert Settings",
        key = "aredn.@alerts[0].pollrate",
        type = "string",
        desc = "<b>Alert Message Pollrate</b> - how many hours to wait between polling for new AREDN Alerts<br><br><small>aredn.@alerts[0].pollrate</small>",
        default = "12",
        needreboot = true
    },
    {
        category = "AREDN Alert Settings",
        key = "aredn.aam.purge",
        type = "none",
        desc = "<b>Alert Message Purge</b> - execute to immediately delete all alerts from this node<br><br><small>aredn.aam.purge</small>",
        default = "",
        postcallback = "alert_purge()"
    }
}
