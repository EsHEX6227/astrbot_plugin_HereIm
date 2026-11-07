# 网络指纹采集：输出一段 JSON，供 AstrBot 插件读取
# 由 astrbot_plugin_HereIm 调用，事件驱动，不常驻

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "SilentlyContinue"

# 1) 只有"有默认网关"的那个接口才算数（排除 WLAN 4/5 虚拟热点网卡、Tailscale）
$routes  = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue
$default = $routes | Sort-Object RouteMetric | Select-Object -First 1
if (-not $default) {
    '{"error":"no-default-route"}'
    exit 0
}
$ifIndex = $default.InterfaceIndex

$ipInfo = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $ifIndex -ErrorAction SilentlyContinue |
          Where-Object { $_.IPAddress -notlike "169.254.*" } |
          Select-Object -First 1
$ip     = $ipInfo.IPAddress
$prefix = $ipInfo.PrefixLength
$gw     = ($routes | Where-Object { $_.InterfaceIndex -eq $ifIndex } | Sort-Object RouteMetric | Select-Object -First 1).NextHop
$adapter = Get-NetAdapter -InterfaceIndex $ifIndex -ErrorAction SilentlyContinue

# 2) 无线信息（有线连接时这些都是空）
$wlan  = netsh wlan show interfaces 2>$null
$ssid  = ($wlan | Select-String -Pattern "^\s+SSID\s+:\s+(.+)$"     | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } | Select-Object -First 1)
$bssid = ($wlan | Select-String -Pattern "AP BSSID\s+:\s+(.+)$"     | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } | Select-Object -First 1)
$chan  = ($wlan | Select-String -Pattern "^\s+Channel\s+:\s+(.+)$"  | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } | Select-Object -First 1)
$sig   = ($wlan | Select-String -Pattern "^\s+Signal\s+:\s+(.+)$"   | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } | Select-Object -First 1)

# 3) 网关 MAC
$gwMac = $null
if ($gw) {
    $line = (arp -a 2>$null | Select-String -Pattern ("^\s*" + [regex]::Escape($gw) + "\s")) | Select-Object -First 1
    if ($line) {
        $m = [regex]::Match($line.Line, "([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})")
        if ($m.Success) { $gwMac = $m.Groups[1].Value.ToLower() }
    }
}

# 4) 网络地址（按八位组做掩码，避免字节序坑）
$subnet = $null
if ($ip -and $prefix) {
    $oct = $ip.Split(".") | ForEach-Object { [int]$_ }
    $net = @()
    for ($i = 0; $i -lt 4; $i++) {
        $bits = [math]::Min(8, [math]::Max(0, $prefix - $i * 8))
        if ($bits -eq 8) { $net += $oct[$i] }
        elseif ($bits -le 0) { $net += 0 }
        else {
            $mask = 256 - [math]::Pow(2, 8 - $bits)
            $net += ($oct[$i] -band [int]$mask)
        }
    }
    $subnet = ($net -join ".") + "/" + $prefix
}

$result = [ordered]@{
    ssid       = $ssid
    bssid      = $bssid
    channel    = $chan
    signal     = $sig
    ip         = $ip
    prefix     = $prefix
    subnet     = $subnet
    gateway    = $gw
    gatewayMac = $gwMac
    adapter    = $adapter.InterfaceDescription
    adapterMac = $adapter.MacAddress
    time       = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
}

$result | ConvertTo-Json -Depth 3 -Compress
