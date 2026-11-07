"""部署报点 · 网络变化时向白名单会话报告实例所在位置

设计约束（按需求定的）：
1. 不调用任何大模型：不注册 LLM 工具、不碰 on_llm_request，因此不影响前缀缓存。
2. 不轮询网络：触发点是 IP 变化（iphlpapi.NotifyAddrChange）、QQ 重连（NapCat lifecycle.connect）、
   启动后一次、以及手动 /报点。判定本身才去读一次网络指纹。
3. 判定分层：先认网络（SSID / 网段 / BSSID / 网关 MAC），命中 locations.json 才报具体地点，
   否则按校园网未标定 / 热点 / 未知 三档降级。
4. 幂等：同一指纹在静默期内不重复上报，state.json 跨重启保留。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession

PLUGIN_DIR = Path(__file__).resolve().parent
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 种子地点。默认留空：地点由 /标定 现场录入，代码里不放任何真实网络标识。
# 若要预置，按下面的格式往列表里加（匹配规则：给出的键必须同时成立）：
#   {"id": "dorm", "name": "宿舍", "match": {"ssid": "MyWiFi", "subnet": "192.168.1.0/24"}}
#   {"id": "room1", "name": "教学楼A201", "match": {"ssid": "Campus", "bssid": "aa:bb:cc:dd:ee:ff"}}
DEFAULT_LOCATIONS: list[dict] = []

def _norm_mac(value: str | None) -> str:
    return (value or "").replace("-", ":").strip().lower()


@register(
    "hereim",
    "EsHEX6227",
    "部署报点：网络变化时向白名单会话报告实例位置（不调用模型，事件驱动）",
    "1.0.0",
)
class HereIm(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg: dict = dict(config or {})
        self.data_dir = self._resolve_data_dir()
        self.locations_path = self.data_dir / "locations.json"
        self.state_path = self.data_dir / "state.json"

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._watch_thread: threading.Thread | None = None
        self._pending: asyncio.Task | None = None
        self._subscribed = False
        self._last_check_lock = asyncio.Lock()

        self.locations = self._load_locations()
        self.state = self._load_state()

    # ---------------- 基础 ----------------

    def _resolve_data_dir(self) -> Path:
        try:
            path = Path(StarTools.get_data_dir("astrbot_plugin_HereIm"))
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            fallback = PLUGIN_DIR / "_data"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _cfg(self, key: str, default):
        value = self.cfg.get(key, default)
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return value

    def _load_locations(self) -> list[dict]:
        if self.locations_path.exists():
            try:
                data = json.loads(self.locations_path.read_text(encoding="utf-8"))
                items = data.get("locations") if isinstance(data, dict) else data
                if isinstance(items, list):
                    return [x for x in items if isinstance(x, dict) and x.get("name")]
            except Exception as exc:
                logger.warning(f"[HereIM] locations.json 读取失败，改用种子数据: {exc}")
        return [dict(x) for x in DEFAULT_LOCATIONS]

    def _save_locations(self) -> None:
        payload = {"version": 1, "locations": self.locations}
        self.locations_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        return {}

    def _save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        self._loop = asyncio.get_running_loop()

        if bool(self._cfg("ip_watch", True)):
            self._start_ip_watcher()

        # 平台适配器可能还没就绪，稍后再订阅一次
        asyncio.create_task(self._subscribe_qq_connect())

        if bool(self._cfg("report_on_startup", True)):
            asyncio.create_task(self._delayed_check("startup", extra_delay=10))
        logger.info(
            f"[HereIM] 已启动：地点 {len(self.locations)} 个，"
            f"白名单 {len(self._targets())} 个，数据目录 {self.data_dir}"
        )

    async def terminate(self) -> None:
        self._stopping = True
        if self._pending and not self._pending.done():
            self._pending.cancel()
        logger.info("[HereIM] 已停止")

    # ---------------- 触发源 ----------------

    def _targets(self) -> list[str]:
        raw = str(self._cfg("targets", "") or "")
        return [x.strip() for x in re.split(r"[,;\n]+", raw) if x.strip()]

    def _start_ip_watcher(self) -> None:
        if os.name != "nt":
            logger.info("[HereIM] 非 Windows，跳过 IP 变化监听")
            return

        def runner() -> None:
            try:
                import ctypes

                iphlpapi = ctypes.WinDLL("iphlpapi")
                iphlpapi.NotifyAddrChange.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                iphlpapi.NotifyAddrChange.restype = ctypes.c_ulong
            except Exception as exc:
                logger.warning(f"[HereIM] 无法加载 iphlpapi，IP 变化监听关闭: {exc}")
                return

            while not self._stopping:
                try:
                    iphlpapi.NotifyAddrChange(None, None)  # 阻塞直到 IP 变化
                except Exception as exc:
                    logger.warning(f"[HereIM] NotifyAddrChange 异常，停止监听: {exc}")
                    return
                if self._stopping:
                    return
                loop = self._loop
                if loop and not loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        self._delayed_check("ip-change"), loop
                    )

        self._watch_thread = threading.Thread(
            target=runner, name="hereim-ipwatch", daemon=True
        )
        self._watch_thread.start()

    async def _subscribe_qq_connect(self) -> None:
        platform_id = str(self._cfg("platform_id", ""))
        for _ in range(6):
            if self._stopping or self._subscribed:
                return
            try:
                inst = self.context.get_platform_inst(platform_id)
                bot = getattr(inst, "bot", None) if inst else None
                if bot is not None and hasattr(bot, "subscribe"):
                    bot.subscribe("meta_event.lifecycle.connect", self._on_qq_connect)
                    self._subscribed = True
                    logger.info("[HereIM] 已订阅 NapCat 连接事件")
                    return
            except Exception as exc:
                logger.debug(f"[HereIM] 订阅连接事件失败（稍后重试）: {exc}")
            await asyncio.sleep(5)
        logger.info("[HereIM] 未订阅到连接事件，只保留 IP 变化 / 手动触发")

    async def _on_qq_connect(self, _event=None) -> None:
        await self._delayed_check("qq-connect", extra_delay=20)

    async def _delayed_check(self, reason: str, extra_delay: int | None = None) -> None:
        settle = int(self._cfg("settle_seconds", 12) or 12)
        delay = settle + (extra_delay if extra_delay is not None else 0)
        if self._pending and not self._pending.done():
            self._pending.cancel()
        try:
            await asyncio.sleep(delay)
            await self._evaluate(reason)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(f"[HereIM] 判定失败（{reason}）: {exc}")

    # ---------------- 网络指纹 ----------------

    def _probe(self) -> dict | None:
        ps = PLUGIN_DIR / "net_probe.ps1"
        if not ps.exists():
            logger.error(f"[HereIM] 缺少 {ps}")
            return None
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ps),
                ],
                capture_output=True,
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            logger.warning(f"[HereIM] 采集网络指纹失败: {exc}")
            return None

        raw = proc.stdout or b""
        text = ""
        for enc in ("utf-8", "gbk"):
            try:
                text = raw.decode(enc).strip()
                break
            except Exception:
                continue
        if not text:
            return None
        try:
            data = json.loads(text.splitlines()[-1])
        except Exception:
            logger.warning(f"[HereIM] 指纹输出无法解析: {text[:300]}")
            return None
        if isinstance(data, dict) and data.get("error"):
            logger.info(f"[HereIM] 当前没有可用网络: {data.get('error')}")
            return None
        return data

    # ---------------- 判定 ----------------

    def _fingerprint(self, fp: dict) -> str:
        return "|".join(
            [
                str(fp.get("ssid") or "").strip().lower(),
                str(fp.get("subnet") or "").strip(),
                _norm_mac(fp.get("bssid")),
            ]
        )

    def _match(self, fp: dict) -> dict | None:
        best: dict | None = None
        best_score = -1
        for loc in self.locations:
            rules = loc.get("match") or {}
            if not isinstance(rules, dict) or not rules:
                continue
            score = 0
            matched = True
            for key, want in rules.items():
                if not want:
                    continue
                if key == "ssid":
                    if str(fp.get("ssid") or "").strip().lower() != str(want).strip().lower():
                        matched = False
                        break
                    score += 1
                elif key == "subnet":
                    if str(fp.get("subnet") or "").strip() != str(want).strip():
                        matched = False
                        break
                    score += 2
                elif key == "bssid":
                    if _norm_mac(fp.get("bssid")) != _norm_mac(str(want)):
                        matched = False
                        break
                    score += 4
                elif key == "gatewayMac":
                    if _norm_mac(fp.get("gatewayMac")) != _norm_mac(str(want)):
                        matched = False
                        break
                    score += 3
                else:
                    continue
            if matched and score > best_score:
                best_score = score
                best = loc
        return best

    def _is_campus(self, fp: dict) -> bool:
        ssid = str(fp.get("ssid") or "").strip().lower()
        if not ssid:
            return False
        raw = str(self._cfg("campus_ssids", "") or "")
        for item in re.split(r"[,;\n]+", raw):
            item = item.strip().lower()
            if item and ssid.startswith(item):
                return True
        return False

    def _format(self, fp: dict, loc: dict | None) -> str:
        inst = str(self._cfg("instance_name", "Bot"))
        netaddr = fp.get("ip") or fp.get("subnet") or "未知地址"
        if loc:
            return f"【{inst}】已部署 · {loc.get('name')} · {netaddr}"
        ssid = str(fp.get("ssid") or "").strip()
        if self._is_campus(fp):
            return f"【{inst}】已部署 · 校园网（未确定教室）· {netaddr}"
        if ssid:
            return f"【{inst}】部署位置不能确定（{ssid}）"
        return f"【{inst}】部署位置不能确定（有线/未知网络 · {netaddr}）"

    def _should_report(self, fingerprint: str, force: bool) -> bool:
        if force:
            return True
        hours = float(self._cfg("cooldown_hours", 6) or 0)
        if hours <= 0:
            return True
        last = self.state.get("last") or {}
        if last.get("fingerprint") == fingerprint:
            try:
                age = time.time() - float(last.get("ts") or 0)
            except Exception:
                age = 1e9
            if age < hours * 3600:
                return False
        return True

    # ---------------- 发送 ----------------

    async def _send_one(self, umo: str, text: str) -> bool:
        try:
            session = MessageSession.from_str(umo)
        except Exception as exc:
            logger.warning(f"[HereIM] 目标会话格式不对，跳过 {umo}: {exc}")
            return False
        try:
            ok = await self.context.send_message(session, MessageChain().message(text))
        except Exception as exc:
            logger.warning(f"[HereIM] 发送到 {umo} 失败: {exc}")
            return False
        if not ok:
            logger.warning(f"[HereIM] 平台未就绪，发送到 {umo} 未成功")
        return bool(ok)

    async def _broadcast(self, text: str) -> list[str]:
        targets = self._targets()
        if not targets:
            logger.warning("[HereIM] 白名单为空，不发送，只记录日志")
            logger.info(f"[HereIM] 本该发送：{text}")
            return []
        sent: list[str] = []
        failed: list[str] = []
        for umo in targets:
            (sent if await self._send_one(umo, text) else failed).append(umo)
        if failed:
            asyncio.create_task(self._retry_send(failed, text))
        if sent:
            logger.info(f"[HereIM] 已发送 {len(sent)}/{len(targets)}：{text}")
        return sent

    async def _retry_send(self, targets: list[str], text: str) -> None:
        await asyncio.sleep(30)
        for umo in list(targets):
            if await self._send_one(umo, text):
                logger.info(f"[HereIM] 重试成功: {umo}")
            else:
                logger.warning(f"[HereIM] 重试仍失败: {umo}")

    # ---------------- 主流程 ----------------

    async def _evaluate(self, reason: str, force: bool = False) -> str | None:
        if not bool(self._cfg("enabled", True)):
            return None
        if self._last_check_lock.locked():
            return None
        async with self._last_check_lock:
            fp = await asyncio.to_thread(self._probe)
            if not fp:
                return None
            loc = self._match(fp)
            fingerprint = self._fingerprint(fp)
            text = self._format(fp, loc)
            if not self._should_report(fingerprint, force):
                logger.debug(f"[HereIM] {reason}: 同一网络静默期内，跳过")
                return None
            self.state["last"] = {
                "fingerprint": fingerprint,
                "place": (loc or {}).get("name"),
                "ts": time.time(),
                "ip": fp.get("ip"),
                "ssid": fp.get("ssid"),
            }
            self._save_state()
            await self._broadcast(text)
            return text

    # ---------------- 命令 ----------------

    @staticmethod
    def _command_arg(event: AstrMessageEvent, command: str) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        text = re.sub(rf"^[/／]?\s*{re.escape(command)}\s*", "", text)
        return text.strip()

    @filter.command("报点")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_report(self, event: AstrMessageEvent):
        await event.send(MessageChain().message("收到，判定中…"))
        text = await self._evaluate("manual", force=True)
        if text:
            await event.send(MessageChain().message(f"已上报：{text}"))
        else:
            await event.send(
                MessageChain().message("没判出来：可能没拿到网络指纹，或白名单为空")
            )

    @filter.command("标定")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_calibrate(self, event: AstrMessageEvent):
        name = self._command_arg(event, "标定")
        if not name:
            await event.send(
                MessageChain().message(
                    "用法：/标定 五号教学楼C516\n"
                    "会在当前位置记录一条地点，之后自动上报"
                )
            )
            return

        fp = await asyncio.to_thread(self._probe)
        if not fp or not fp.get("ssid"):
            await event.send(
                MessageChain().message("现在拿不到无线网络指纹（有线连接或采集失败），没法标定")
            )
            return

        match: dict[str, str] = {"ssid": str(fp.get("ssid")).strip()}
        if fp.get("subnet"):
            match["subnet"] = str(fp.get("subnet")).strip()
        # 校园网下同一网段有多个教室，用 BSSID 才能区分；其它网络（自己家/热点）不加 BSSID，
        # 免得路由器重启换了 BSSID 就认不出来
        if self._is_campus(fp) and fp.get("bssid"):
            match["bssid"] = _norm_mac(fp.get("bssid"))

        self.locations = [x for x in self.locations if x.get("name") != name]
        loc = {
            "id": f"loc{int(time.time())}",
            "name": name,
            "match": match,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.locations.append(loc)
        self._save_locations()

        detail = "，".join(f"{k}={v}" for k, v in match.items())
        await event.send(
            MessageChain().message(f"已录入「{name}」\n匹配条件：{detail}\n下次连上这条网络就会自动上报")
        )

    @filter.command("地点")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_locations(self, event: AstrMessageEvent):
        if not self.locations:
            await event.send(MessageChain().message("还没有任何地点"))
            return
        lines = [f"已录入 {len(self.locations)} 个地点："]
        for loc in self.locations:
            rules = loc.get("match") or {}
            detail = "，".join(f"{k}={v}" for k, v in rules.items())
            lines.append(f"· {loc.get('name')}（{detail}）")
        await event.send(MessageChain().message("\n".join(lines)))
