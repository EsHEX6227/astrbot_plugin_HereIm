# HereIM

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.23-6A5ACD)

AstrBot 插件：网络一变，就向白名单会话报一句「实例已部署于某地」。

> An AstrBot plugin that reports where the instance is deployed to a whitelist of sessions
> whenever the network changes — no LLM calls, no prompt injection, no polling.

- **不调用大模型**：不注册 LLM 工具、不注入提示词，因此不影响前缀缓存。
- **不轮询**：触发点是 IP 变化、QQ 重连、启动后一次、手动命令。
- **可现场标定**：走到新地点发一句 `/标定 <地点名>`，之后连上这条网络就自动报。

## 工作原理

```
触发（IP 变化 / QQ 重连 / 启动 / 手动）
   ↓ 等 settle_seconds（默认 12 秒，让 DHCP 与 BSSID 稳定）
采集网络指纹（PowerShell：SSID、BSSID、网段、网关 MAC、当前 IP）
   ↓
按 locations.json 逐条匹配（给出的条件必须同时成立，取最具体的一条）
   ↓
命中 → 报具体地点；未命中 → 校园网未标定 / 热点 / 未知网络 三档降级
   ↓
幂等检查（同指纹在 cooldown_hours 内不重复）→ 发送到白名单会话
```

报文示例：

| 情况 | 报文 |
|---|---|
| 命中已录入地点 | `【Bot】已部署 · 宿舍 · 192.168.1.23` |
| 校园网但该教室没录 | `【Bot】已部署 · 校园网（未确定教室）· 10.20.31.7` |
| 热点 / 未知无线 | `【Bot】部署位置不能确定（MyPhone）` |
| 有线 / 取不到 SSID | `【Bot】部署位置不能确定（有线/未知网络 · 10.0.0.5）` |

## 安装

1. 把整个目录放到 `AstrBot/data/plugins/astrbot_plugin_HereIm/`；
2. 重启 AstrBot（或在插件页重载）；
3. 到配置页填 `targets`（白名单会话）、`platform_id`（平台适配器 ID）；
4. 走到一个地点，发 `/标定 宿舍`，把当前网络录进去。

依赖：Windows + PowerShell（采集指纹）、`iphlpapi`（监听 IP 变化）。非 Windows 只剩启动判定与手动命令。

## 命令（仅管理员）

| 命令 | 作用 |
|---|---|
| `/报点` | 立刻判定并上报一次（无视冷却） |
| `/标定 <地点名>` | 把**当前**网络录成一条地点 |
| `/地点` | 列出已录入的地点与匹配条件 |

`/标定` 的匹配条件规则：

- 非校园网（家里/宿舍路由器）：记 `ssid + subnet`。故意不记 BSSID，路由器重启换了 BSSID 也不影响；
- 校园网（由 `campus_ssids` 判定）：记 `ssid + subnet + bssid`。同一网段下有多间教室，只能靠接入的 AP 区分。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `instance_name` | Bot | 报文里的实例名 |
| `platform_id` | 空 | 平台适配器 ID，用于订阅 NapCat 重连事件；留空则不订阅 |
| `targets` | 空 | 白名单 umo，逗号或换行分隔；**留空则谁都不发**，只写日志 |
| `cooldown_hours` | 6 | 同一网络静默期，0 = 每次都报 |
| `settle_seconds` | 12 | 网络变化后等多久再读指纹 |
| `campus_ssids` | 空 | 校园网 SSID 前缀，逗号分隔；留空则不做「校园网未标定」降级 |
| `report_on_startup` | true | 启动后判一次 |
| `ip_watch` | true | IP 变化监听 |

## 数据文件

`data/plugin_data/astrbot_plugin_HereIm/`：

- `locations.json`：地点表（`/标定` 维护，也可手改）
- `state.json`：上次上报的指纹与时间（删掉即重置冷却）

## 已知边界

- **同网段漫游不触发**：Wi-Fi 没断、IP 没变时没有事件，需要 `/报点` 手动补。
- **热点 MAC 会随机化**：所以热点只按 SSID 识别，默认落到「不能确定」。
- **DHCP 会换 IP**：所以 IP 只用于展示，不参与判定。
- 路由器被重置（SSID 变了、网关 MAC 变了）后需要重新 `/标定`。
- 地点表里含真实 SSID/BSSID，**不要把它提交到公开仓库**（本仓库的 `.gitignore` 已排除运行时数据）。

## 许可证

MIT，见 [LICENSE](LICENSE)。
