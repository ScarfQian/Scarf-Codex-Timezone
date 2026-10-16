#!/usr/bin/env python3
"""
出口检测与页面 Intl 时区验证原型。

日期：2026-10-15
创建人：ScarfQian

这个原型只做两件事：
1. 核对 Codex/Magpie 活动路由，通过 chatgpt.com 读取出口 IP 并查询该 IP 的 IANA 时区；
2. 在隔离的 Codex 页面中验证进程级 TZ、CDP 覆盖和恢复值。

它不会退出或修改正在运行的 ChatGPT/Codex，不会修改 macOS 系统时区，也不会修改
Magpie、Clash 或 Codex 配置。隔离实例的验证通过，只能证明新启动的 Codex 进程采用该时区，
不能单独证明当前运行实例的模型请求上下文或自动化保存接口已改变。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone as dt_timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_PROXY = "http://127.0.0.1:7897"
DEFAULT_GEO_ENDPOINTS = (
    "https://ipwho.is/",
    "https://ipinfo.io/json",
)
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_APP_CANDIDATES = tuple(
    root / name / "Contents" / "MacOS" / binary
    for root in (Path("/Applications"), Path.home() / "Applications")
    for name, binary in (("Codex.app", "Codex"), ("ChatGPT.app", "ChatGPT"))
)
DEFAULT_CODEX_APP = str(next((path for path in _APP_CANDIDATES if path.is_file()), _APP_CANDIDATES[0]))
DEFAULT_MIHOMO_SOCKET = f"/var/run/clash-verge-service/users/{os.getuid()}/verge-mihomo.sock"
CODEX_TRACE_URL = "https://chatgpt.com/cdn-cgi/trace"


class PrototypeError(RuntimeError):
    """Expected, user-actionable prototype failure."""


@dataclass(frozen=True)
class EgressInfo:
    ip: str
    country_code: str
    country: str
    city: str
    timezone: str
    endpoint: str
    verified_endpoints: tuple[str, ...] = ()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="复现 Codex/Magpie 出口并验证隔离 Codex 页面 Intl 时区。"
    )
    parser.add_argument("--proxy", default=DEFAULT_PROXY, help=f"HTTP 代理（默认：{DEFAULT_PROXY}）")
    parser.add_argument(
        "--route",
        choices=("codex", "proxy"),
        default="codex",
        help="出口探测路径；codex 读取 chatgpt.com 路径出口，proxy 只测通用查询路径",
    )
    parser.add_argument("--mihomo-socket", default=DEFAULT_MIHOMO_SOCKET, help="Mihomo 控制 Unix socket")
    parser.add_argument("--codex-group", default="GPT", help="OpenAI 域名使用的 Mihomo 代理组")
    parser.add_argument("--timeout", type=float, default=12.0, help="每个网络请求超时秒数")
    parser.add_argument(
        "--endpoint",
        action="append",
        dest="endpoints",
        help="公网 IP/时区服务，可重复指定；默认使用 ipwho.is 与 ipinfo.io",
    )
    parser.add_argument("--chrome", default=DEFAULT_CHROME, help="Chromium/Chrome 可执行文件路径")
    parser.add_argument("--codex-app", default=DEFAULT_CODEX_APP, help="Codex 桌面应用可执行文件路径")
    parser.add_argument(
        "--target",
        choices=("codex", "chrome"),
        default="codex",
        help="验证目标；默认启动隔离 Codex，chrome 仅用于通用 CDP 验证",
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    parser.add_argument("--self-test", action="store_true", help="只运行本地解析与校验测试")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    return args


def _proxy_request(url: str, proxy: str, timeout: float) -> bytes:
    # -q 禁用用户 curlrc；--noproxy '' 强制查询服务使用指定代理，避免 NO_PROXY 旁路。
    command = [
        "/usr/bin/curl", "-q", "--silent", "--show-error", "--fail",
        "--proxy", proxy, "--noproxy", "", "--max-time", str(timeout),
        "--header", "Accept: application/json", "--user-agent", "Codex-IP-Timezone/0.2",
        url,
    ]
    try:
        response = subprocess.run(command, capture_output=True, timeout=timeout + 2, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise PrototypeError(f"无法通过代理访问 {url}: {exc}") from exc
    if response.returncode:
        raise PrototypeError(f"无法通过代理访问 {url}: {response.stderr.decode('utf-8', errors='replace').strip()}")
    return response.stdout


def _json_request(url: str, proxy: str, timeout: float) -> dict[str, Any]:
    payload = _proxy_request(url, proxy, timeout)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrototypeError(f"{url} 返回的内容不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise PrototypeError(f"{url} 返回的 JSON 不是对象")
    return value


def _normalize_geo(value: dict[str, Any], endpoint: str) -> EgressInfo:
    if endpoint.startswith("https://ipwho.is"):
        if value.get("success") is False:
            raise PrototypeError(f"ipwho.is 返回失败：{value.get('message', 'unknown error')}")
        ip = value.get("ip")
        country_code = value.get("country_code")
        country = value.get("country")
        city = value.get("city")
        timezone_value = value.get("timezone")
        timezone = timezone_value.get("id") if isinstance(timezone_value, dict) else None
    else:
        ip = value.get("ip")
        country_code = value.get("country")
        country = value.get("country")
        city = value.get("city")
        timezone = value.get("timezone")

    if not isinstance(ip, str):
        raise PrototypeError(f"{endpoint} 缺少公网 IP")
    try:
        address = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise PrototypeError(f"{endpoint} 返回了非法 IP：{ip!r}") from exc
    if not address.is_global:
        raise PrototypeError(f"{endpoint} 返回的不是公网 IP：{ip}")
    if not isinstance(country_code, str) or not country_code:
        raise PrototypeError(f"{endpoint} 缺少国家代码")
    if not isinstance(timezone, str) or not timezone:
        raise PrototypeError(f"{endpoint} 缺少 IANA 时区")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PrototypeError(f"{endpoint} 返回的不是本机可识别的 IANA 时区：{timezone}") from exc

    return EgressInfo(
        ip=ip,
        country_code=country_code.upper(),
        country=str(country or ""),
        city=str(city or ""),
        timezone=timezone,
        endpoint=endpoint,
    )


def _geolocation_url(endpoint: str, ip: str) -> str:
    if "{ip}" in endpoint:
        return endpoint.replace("{ip}", ip)
    if endpoint.rstrip("/") == "https://ipwho.is":
        return f"https://ipwho.is/{ip}"
    if endpoint.rstrip("/") in {"https://ipinfo.io", "https://ipinfo.io/json"}:
        return f"https://ipinfo.io/{ip}/json"
    raise PrototypeError("按 IP 查询时，额外地理服务地址须包含 {ip} 占位符")


def detect_egress(proxy: str, timeout: float, endpoints: tuple[str, ...], expected_ip: str | None = None) -> EgressInfo:
    observations: list[EgressInfo] = []
    errors: list[str] = []
    for endpoint in endpoints:
        try:
            url = _geolocation_url(endpoint, expected_ip) if expected_ip else endpoint
            observation = _normalize_geo(_json_request(url, proxy, timeout), url)
            if expected_ip and ipaddress.ip_address(observation.ip) != ipaddress.ip_address(expected_ip):
                raise PrototypeError(f"{url} 未返回指定的目标 IP")
            observations.append(observation)
        except PrototypeError as exc:
            errors.append(str(exc))
    if not observations:
        raise PrototypeError("所有公网出口查询均失败：" + "；".join(errors))
    reference = observations[0]
    disagreements = [
        f"{item.endpoint}: ip={item.ip}, timezone={item.timezone}"
        for item in observations[1:]
        if (item.ip, item.country_code, item.timezone)
        != (reference.ip, reference.country_code, reference.timezone)
    ]
    if disagreements:
        raise PrototypeError(
            "地理服务对同一探测路径返回不一致结果："
            + "; ".join([f"{reference.endpoint}: ip={reference.ip}, timezone={reference.timezone}", *disagreements])
        )
    return replace(reference, verified_endpoints=tuple(item.endpoint for item in observations))


def _mihomo_json(socket_path: str, path: str, timeout: float) -> dict[str, Any]:
    if not Path(socket_path).exists():
        raise PrototypeError(f"找不到 Mihomo 控制 socket：{socket_path}")
    command = [
        "/usr/bin/curl", "-q", "--silent", "--show-error", "--fail", "--noproxy", "*",
        "--unix-socket", socket_path, "--max-time", str(timeout), f"http://localhost{path}",
    ]
    try:
        response = subprocess.run(command, capture_output=True, timeout=timeout + 2, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise PrototypeError(f"读取 Mihomo 控制接口失败：{exc}") from exc
    if response.returncode:
        raise PrototypeError(f"读取 Mihomo 控制接口失败：{response.stderr.decode('utf-8', errors='replace').strip()}")
    try:
        value = json.loads(response.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrototypeError("Mihomo 控制接口返回的内容不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise PrototypeError("Mihomo 控制接口返回的 JSON 不是对象")
    return value


def resolve_mihomo_chain(socket_path: str, group: str, timeout: float) -> tuple[list[str], str]:
    chain: list[str] = []
    current = group
    seen: set[str] = set()
    group_types = {"Selector", "Fallback", "URLTest"}
    while current not in seen:
        seen.add(current)
        value = _mihomo_json(socket_path, f"/proxies/{quote(current, safe='')}", timeout)
        chain.append(current)
        if value.get("type") in {"LoadBalance", "Relay"}:
            raise PrototypeError(f"代理组 {current} 采用负载均衡或串联，无法用单一叶节点代表实际出口")
        if value.get("type") not in group_types:
            return chain, current
        next_name = value.get("now")
        if not isinstance(next_name, str) or not next_name:
            raise PrototypeError(f"Mihomo 代理组 {current} 没有当前选择")
        current = next_name
    raise PrototypeError("Mihomo 代理组解析出现循环：" + " → ".join(chain))


def verify_codex_route(socket_path: str, group: str, leaf: str, timeout: float) -> None:
    """核对当前规则与已建立的模型连接，防止检测已不再使用的代理组。"""
    config = _mihomo_json(socket_path, "/configs", timeout)
    if str(config.get("mode", "")).lower() != "rule":
        raise PrototypeError("Clash 当前不是规则模式，不能按 OpenAI 代理组推断 Codex 出口")
    rules = _mihomo_json(socket_path, "/rules", timeout).get("rules", [])
    for domain in ("openai.com", "chatgpt.com"):
        matches = [r for r in rules if r.get("type") == "DomainSuffix" and r.get("payload") == domain and not (r.get("extra") or {}).get("disabled")]
        if not matches or matches[0].get("proxy") != group:
            raise PrototypeError(f"当前 {domain} 规则未指向 {group}，未使用未经确认的出口")
    connections = _mihomo_json(socket_path, "/connections", timeout).get("connections", [])
    for connection in connections:
        meta = connection.get("metadata", {})
        if meta.get("process") not in {"magpie", "Codex (Service)", "codex", "Codex", "ChatGPT"}:
            continue
        if meta.get("host") not in {"chatgpt.com", "api.openai.com"}:
            continue
        active_chain = connection.get("chains") or []
        if active_chain and active_chain[0] != leaf:
            raise PrototypeError("Codex/Magpie 仍有连接使用另一出口；待相关连接结束后再同步时区")


def _read_chatgpt_ip(proxy: str, timeout: float) -> str:
    try:
        trace = _proxy_request(CODEX_TRACE_URL, proxy, timeout).decode("utf-8")
        fields = dict(line.split("=", 1) for line in trace.splitlines() if "=" in line)
        ip = ipaddress.ip_address(fields.get("ip", ""))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PrototypeError("chatgpt.com 的网络诊断响应没有有效出口 IP，未推测时区") from exc
    if fields.get("h") != "chatgpt.com" or not ip.is_global:
        raise PrototypeError("chatgpt.com 网络诊断响应无效，未推测时区")
    return str(ip)


def detect_codex_egress(
    socket_path: str,
    group: str,
    proxy: str,
    timeout: float,
    endpoints: tuple[str, ...],
) -> tuple[EgressInfo, dict[str, Any]]:
    chain, leaf = resolve_mihomo_chain(socket_path, group, timeout)
    verify_codex_route(socket_path, group, leaf, timeout)
    ip = _read_chatgpt_ip(proxy, timeout)
    egress = detect_egress(proxy, timeout, endpoints, expected_ip=ip)
    chain_after, leaf_after = resolve_mihomo_chain(socket_path, group, timeout)
    if chain_after != chain or leaf_after != leaf:
        raise PrototypeError("Codex/Magpie 代理链在出口探测期间发生变化，未使用旧时区")
    verify_codex_route(socket_path, group, leaf, timeout)
    return egress, {
        "mode": "chatgpt_domain_trace",
        "proxy_group": group,
        "chain": chain,
        "leaf": leaf,
        "trace_url": CODEX_TRACE_URL,
    }


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = b"<!doctype html><meta charset='utf-8'><title>Codex timezone probe</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _WebSocket:
    """Small dependency-free CDP WebSocket client for the prototype."""

    def __init__(self, url: str, timeout: float = 8.0) -> None:
        if not url.startswith("ws://"):
            raise PrototypeError(f"原型只接受 ws:// CDP 地址：{url}")
        host_port_path = url[5:]
        authority, path = host_port_path.split("/", 1)
        host, port_text = authority.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port_text)), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buffer = bytearray()
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = self._read_until(b"\r\n\r\n")
        header = response.decode("latin1", errors="replace")
        if " 101 " not in header.splitlines()[0]:
            self.close()
            raise PrototypeError(f"CDP WebSocket 握手失败：{header.splitlines()[0]}")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if f"sec-websocket-accept: {expected.lower()}" not in header.lower():
            self.close()
            raise PrototypeError("CDP WebSocket 握手校验失败")

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise PrototypeError("CDP WebSocket 在握手中途关闭")
            self._buffer.extend(chunk)
        end = self._buffer.index(marker) + len(marker)
        data = bytes(self._buffer[:end])
        del self._buffer[:end]
        return data

    def _read_exact(self, length: int) -> bytes:
        # TCP 的单次 recv 不保证返回完整帧头；保留握手后同批到达的帧数据。
        while len(self._buffer) < length:
            chunk = self.sock.recv(length - len(self._buffer))
            if not chunk:
                raise PrototypeError("CDP WebSocket 在消息中途关闭")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:length])
        del self._buffer[:length]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        first_two = self._read_exact(2)
        first, second = first_two
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        masked = second & 0x80
        mask = self._read_exact(4) if masked else b""
        data = bytearray(self._read_exact(length))
        if masked:
            data = bytearray(byte ^ mask[index % 4] for index, byte in enumerate(data))
        return opcode, bytes(data)

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = getattr(self, "_request_id", 0) + 1
        self._request_id = request_id
        message = json.dumps({"id": request_id, "method": method, "params": params or {}}, separators=(",", ":"))
        self._send_frame(1, message.encode("utf-8"))
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 8:
                raise PrototypeError("CDP WebSocket 已关闭")
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode != 1:
                continue
            value = json.loads(payload.decode("utf-8"))
            if value.get("id") == request_id:
                if "error" in value:
                    raise PrototypeError(f"CDP {method} 失败：{value['error']}")
                return value.get("result", {})

    def close(self) -> None:
        try:
            self._send_frame(8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _read_cdp_json(port: int, path: str, timeout: float) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    # CDP 只连接本机；显式禁用环境代理，不依赖用户的 NO_PROXY 配置。
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrototypeError(f"无法读取 CDP {url}: {exc}") from exc


def _wait_cdp(port: int, timeout: float, url_prefix: str | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            version = _read_cdp_json(port, "/json/version", min(1.0, timeout))
            targets = _read_cdp_json(port, "/json/list", min(1.0, timeout))
            page = next((item for item in targets if item.get("type") == "page" and item.get("webSocketDebuggerUrl") and (url_prefix is None or item.get("url", "").startswith(url_prefix))), None)
            if page:
                return {"version": version, "target": page}
            last_error = "CDP 已启动但没有 page target"
        except PrototypeError as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise PrototypeError(f"等待 CDP 超时：{last_error}")


def _evaluate(ws: _WebSocket, expression: str) -> Any:
    result = ws.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    exception = result.get("exceptionDetails")
    if exception:
        raise PrototypeError(f"页面 JavaScript 执行失败：{exception}")
    return result.get("result", {}).get("value")


def _page_snapshot(ws: _WebSocket) -> dict[str, Any]:
    value = _evaluate(
        ws,
        "({timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, "
        "...(() => {const d = new Date(); const pad = n => String(n).padStart(2, '0'); "
        "return {timestampMs: d.getTime(), date: d.toString(), "
        "localDate: d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()), "
        "offsetMinutes: d.getTimezoneOffset()};})()})",
    )
    if not isinstance(value, dict):
        raise PrototypeError("页面没有返回有效的 Intl 快照")
    return value


def _check_snapshot(value: dict[str, Any], timezone: str) -> dict[str, bool]:
    timestamp = value.get("timestampMs")
    if not isinstance(timestamp, (int, float)):
        raise PrototypeError("页面快照缺少有效时间戳")
    local = datetime.fromtimestamp(timestamp / 1000, dt_timezone.utc).astimezone(ZoneInfo(timezone))
    return {
        "timezone": value.get("timezone") == timezone,
        "local_date": value.get("localDate") == local.strftime("%Y-%m-%d"),
        "utc_offset": value.get("offsetMinutes") == -local.utcoffset().total_seconds() / 60,
    }


def _run_isolated_app(
    app_path: str,
    timezone: str,
    timeout: float,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    app = Path(app_path)
    if not app.is_file() or not os.access(app, os.X_OK):
        raise PrototypeError(f"找不到可执行的应用：{app}")

    root = tempfile.TemporaryDirectory(prefix="codex-ip-timezone-app-")
    root_path = Path(root.name)
    codex_home = root_path / "codex-home"
    user_data = root_path / "app-data"
    codex_home.mkdir()
    user_data.mkdir()
    port = _find_free_port()
    process: subprocess.Popen[str] | None = None
    ws: _WebSocket | None = None
    try:
        env = os.environ.copy()
        env.update(
            {
                "TZ": timezone,
                "CODEX_HOME": str(codex_home),
                "CODEX_ELECTRON_USER_DATA_PATH": str(user_data),
            }
        )
        command = [
            str(app),
            f"--user-data-dir={user_data}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=http://127.0.0.1",
            "--codex-browser-background-networking-disabled",
            "--no-first-run",
            *(extra_args or []),
        ]
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        cdp = _wait_cdp(port, timeout, url_prefix="app://")
        ws = _WebSocket(cdp["target"]["webSocketDebuggerUrl"], timeout=timeout)
        baseline = _page_snapshot(ws)
        ws.call("Emulation.setTimezoneOverride", {"timezoneId": "UTC"})
        try:
            overridden = _page_snapshot(ws)
        finally:
            ws.call("Emulation.setTimezoneOverride", {"timezoneId": ""})
        restored = _page_snapshot(ws)
        checks = {
            "process_tz": _check_snapshot(baseline, timezone),
            "override": _check_snapshot(overridden, "UTC"),
            "restore": _check_snapshot(restored, timezone),
        }
        return {
            "status": "PASS" if all(all(stage.values()) for stage in checks.values()) else "FAIL",
            "target": "isolated_codex_app",
            "timezone_requested": timezone,
            "baseline": baseline,
            "overridden": overridden,
            "restored": restored,
            "checks": checks,
            "cdp_browser": cdp["version"].get("Browser"),
            "note": "隔离 Codex 的进程级 TZ 与页面 Intl 已验证；未验证现有实例、模型请求或自动化保存接口。",
        }
    finally:
        if ws is not None:
            ws.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        root.cleanup()


def validate_isolated_page(timezone: str, chrome_path: str, timeout: float) -> dict[str, Any]:
    chrome = Path(chrome_path)
    if not chrome.is_file() or not os.access(chrome, os.X_OK):
        raise PrototypeError(f"找不到可执行的 Chrome/Chromium：{chrome}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    profile = tempfile.TemporaryDirectory(prefix="codex-ip-timezone-chrome-")
    port = _find_free_port()
    process: subprocess.Popen[str] | None = None
    ws: _WebSocket | None = None
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        command = [
            str(chrome),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=http://127.0.0.1",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            f"--user-data-dir={profile.name}",
            url,
        ]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        cdp = _wait_cdp(port, timeout)
        ws = _WebSocket(cdp["target"]["webSocketDebuggerUrl"], timeout=timeout)
        baseline = _page_snapshot(ws)
        ws.call("Emulation.setTimezoneOverride", {"timezoneId": timezone})
        try:
            overridden = _page_snapshot(ws)
        finally:
            ws.call("Emulation.setTimezoneOverride", {"timezoneId": ""})
        restored = _page_snapshot(ws)
        checks = {
            "baseline": _check_snapshot(baseline, baseline["timezone"]),
            "overridden": _check_snapshot(overridden, timezone),
            "restored": _check_snapshot(restored, baseline["timezone"]),
        }
        passed = all(all(stage.values()) for stage in checks.values())
        return {
            "status": "PASS" if passed else "FAIL",
            "target": "isolated_chromium_page",
            "timezone_requested": timezone,
            "baseline": baseline,
            "overridden": overridden,
            "restored": restored,
            "checks": checks,
            "cdp_browser": cdp["version"].get("Browser"),
            "note": "页面覆盖通过不等于当前 Codex 应用请求上下文已改变。",
        }
    finally:
        if ws is not None:
            ws.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        server.shutdown()
        server.server_close()
        profile.cleanup()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_self_test() -> dict[str, Any]:
    valid = _normalize_geo(
        {
            "ip": "8.8.8.8",
            "country_code": "US",
            "country": "United States",
            "city": "Los Angeles",
            "timezone": {"id": "America/Los_Angeles"},
        },
        "https://ipwho.is/",
    )
    failures: list[str] = []
    for bad in (
        {"ip": "not-an-ip", "country_code": "US", "timezone": {"id": "America/Los_Angeles"}},
        {"ip": "8.8.8.8", "country_code": "US", "timezone": {"id": "Not/AZone"}},
    ):
        try:
            _normalize_geo(bad, "https://ipwho.is/")
        except PrototypeError:
            continue
        failures.append("非法地理响应未被拒绝")
    if failures:
        raise PrototypeError("；".join(failures))
    return {"status": "PASS", "valid_timezone": asdict(valid), "rejected_invalid_responses": 2}


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.self_test:
            result = run_self_test()
        else:
            endpoints = tuple(args.endpoints or DEFAULT_GEO_ENDPOINTS)
            if args.route == "codex":
                egress, route = detect_codex_egress(
                    args.mihomo_socket,
                    args.codex_group,
                    args.proxy,
                    args.timeout,
                    endpoints,
                )
            else:
                egress = detect_egress(args.proxy, args.timeout, endpoints)
                route = {"mode": "explicit_proxy", "proxy": args.proxy}
            if egress.country_code != "US":
                raise PrototypeError(
                    f"出口国家不是 US：{egress.country_code} / {egress.country}；为避免误覆盖页面时区，原型已停止。"
                )
            if args.target == "codex":
                page = _run_isolated_app(args.codex_app, egress.timezone, args.timeout)
            else:
                page = validate_isolated_page(egress.timezone, args.chrome, args.timeout)
            result = {
                "status": "PASS" if page["status"] == "PASS" else "FAIL",
                "egress": asdict(egress),
                "route": route,
                "geo_endpoints": list(endpoints),
                "egress_scope": "通过当前代理访问 chatgpt.com 读取的出口 IP；地理服务按该指定 IP 查询。" if args.route == "codex" else "通用代理查询路径，不能代表 Codex/Magpie 模型路径。",
                "page": page,
            }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "PASS" else 1
    except (PrototypeError, OSError, subprocess.SubprocessError) as exc:
        error = {"status": "BLOCKED", "error": str(exc)}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
