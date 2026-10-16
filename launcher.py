#!/usr/bin/env python3
"""
按 Codex/Magpie 当前出口时区启动 ChatGPT/Codex。

日期：2026-10-15
创建人：ScarfQian

启动器设置新进程的 TZ，并只读核验实际页面。支持检查和恢复本机时区，不修改系统时区、
代理配置或 Codex 配置文件。已有实例运行时不替换或关闭它。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
PROTOTYPE_PATH = ROOT / "timezone_probe.py"
SPEC = importlib.util.spec_from_file_location("codex_ip_timezone_prototype", PROTOTYPE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载原型模块：{PROTOTYPE_PATH}")
PROTOTYPE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROTOTYPE
SPEC.loader.exec_module(PROTOTYPE)
SETTINGS_DIR = Path.home() / "Library" / "Application Support" / "Scarf-Codex-Timezone"
STATE_PATH = SETTINGS_DIR / "session.json"
RESTORE_GUIDE = "恢复方法：在 Codex 中按 ⌘Q 完整退出，再从原来的 Codex 图标打开；也可再次打开本工具，选择「恢复本机时区」。仅关闭窗口或关闭本工具提示不会恢复。"
SYSTEM_GUIDE = "当前按本机时区运行。下次可以直接从原来的 Codex 图标打开；关闭本工具提示不会关闭 Codex。"


def _load_config(path: Path, required: bool) -> dict[str, object]:
    if not path.exists() and not required:
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PROTOTYPE.PrototypeError(f"无法读取配置文件 {path}：{exc}") from exc
    allowed = {"codex_app", "proxy", "mihomo_socket", "codex_group", "allowed_countries", "timeout"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise PROTOTYPE.PrototypeError("配置必须是 JSON 对象，仅支持 config.example.json 列出的字段。")
    for name in ("codex_app", "proxy", "mihomo_socket", "codex_group"):
        if name in config and (not isinstance(config[name], str) or not config[name].strip()):
            raise PROTOTYPE.PrototypeError(f"配置字段 {name} 必须是非空字符串；自动选择时请省略该字段。")
    countries = config.get("allowed_countries", ["US"])
    if not isinstance(countries, list) or not countries or any(not isinstance(item, str) for item in countries):
        raise PROTOTYPE.PrototypeError("allowed_countries 必须是国家代码数组，例如 [\"US\"]；允许全部使用 [\"*\"]。")
    timeout = config.get("timeout", 12.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise PROTOTYPE.PrototypeError("配置 timeout 必须是大于零的有限秒数。")
    return config


def parse_args(argv: list[str]) -> argparse.Namespace:
    initial = argparse.ArgumentParser(add_help=False)
    initial.add_argument("--config", type=Path)
    initial_args, _ = initial.parse_known_args(argv)
    # 源码旁的 config.json 便于便携使用；应用包默认读取用户配置目录。
    default_config = ROOT / "config.json" if (ROOT / "config.json").is_file() else SETTINGS_DIR / "config.json"
    config_path = (initial_args.config or default_config).expanduser()
    config = _load_config(config_path, initial_args.config is not None)
    parser = argparse.ArgumentParser(description="按 Codex/Magpie 当前出口时区启动 ChatGPT/Codex")
    parser.add_argument("--config", type=Path, default=config_path, help="JSON 配置路径；命令行参数优先")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-only", action="store_true", help="只检测出口，不启动 Codex")
    action.add_argument("--status", action="store_true", help="只读检查当前实例是否采用目标时区")
    action.add_argument("--restore", action="store_true", help="完整退出 Codex 后，按本机时区重新启动")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--gui", action="store_true", help="在 macOS 对话框中显示结果")
    parser.add_argument("--timeout", type=float, default=config.get("timeout", 12.0), help="单项检查超时秒数")
    parser.add_argument("--mihomo-socket", default=config.get("mihomo_socket", PROTOTYPE.DEFAULT_MIHOMO_SOCKET))
    parser.add_argument("--proxy", default=config.get("proxy", PROTOTYPE.DEFAULT_PROXY))
    parser.add_argument("--codex-group", default=config.get("codex_group", "GPT"))
    parser.add_argument("--codex-app", default=config.get("codex_app", PROTOTYPE.DEFAULT_CODEX_APP))
    parser.add_argument("--countries", default=",".join(config.get("allowed_countries", ["US"])), help="允许的 ISO 国家代码，逗号分隔；* 表示全部，默认 US")
    parser.add_argument("--state-file", type=Path, default=STATE_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是大于 0 的有限秒数")
    args.allowed_countries = {item.strip().upper() for item in args.countries.split(",") if item.strip()}
    if not args.allowed_countries or any(item != "*" and (len(item) != 2 or not item.isascii() or not item.isalpha()) for item in args.allowed_countries):
        parser.error("--countries 需为国家代码（如 US,CA）或 *")
    args.codex_app = str(Path(args.codex_app).expanduser())
    args.mihomo_socket = str(Path(args.mihomo_socket).expanduser())
    return args


def _require_allowed_country(egress: object, args: argparse.Namespace) -> None:
    if "*" not in args.allowed_countries and egress.country_code not in args.allowed_countries:
        raise PROTOTYPE.PrototypeError(f"出口国家 {egress.country_code} 不在允许列表 {','.join(sorted(args.allowed_countries))}；未启动或确认同步。可通过 allowed_countries 配置。")


def _codex_processes(app_path: str) -> dict[int, str]:
    """按主进程的可执行路径判断，避免 macOS pgrep 名称不一致。"""
    target = str(Path(app_path))
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except OSError as exc:
        raise PROTOTYPE.PrototypeError("无法读取 Codex 进程列表，未执行切换或恢复；请检查进程读取权限。") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise PROTOTYPE.PrototypeError("无法确认 Codex 是否正在运行，已停止启动；请检查进程读取权限。")
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and (fields[1] == target or fields[1].startswith(target + " ")):
            processes[int(fields[0])] = fields[1]
    return processes


def _codex_running(app_path: str) -> bool:
    return bool(_codex_processes(app_path))


def _system_timezone() -> str:
    # 直接读取 macOS 时区来源，避免继承到本工具的 TZ 干扰“恢复”。
    path = str(Path("/etc/localtime").resolve())
    if "/zoneinfo/" not in path:
        raise PROTOTYPE.PrototypeError("无法读取本机系统时区，未启动或恢复。")
    value = path.split("/zoneinfo/", 1)[1]
    ZoneInfo(value)
    return value


def _choose_action() -> str | None:
    script = '''set choice to choose from list {"按出口启动", "检查是否成功", "恢复本机时区"} with title "Scarf-Codex-Timezone" with prompt "切换或恢复前，请先在 Codex 中按 ⌘Q 完整退出。关闭本工具不会恢复仍在运行的 Codex 时区。" default items {"按出口启动"} OK button name "执行" cancel button name "取消"
if choice is false then return ""
return item 1 of choice'''
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode:
        if "-128" in result.stderr:
            return None
        raise PROTOTYPE.PrototypeError("无法显示操作菜单；可以使用 --status 或 --restore 命令入口。")
    return {"检查是否成功": "status", "恢复本机时区": "restore", "按出口启动": "switch"}.get(result.stdout.strip())


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(path: Path) -> dict[str, object]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("mode") not in ("egress", "system"):
            raise ValueError("invalid session")
        if not isinstance(state["pid"], int) or not isinstance(state["port"], int) or not 0 < state["port"] < 65536:
            raise ValueError("invalid process")
        if not isinstance(state["app_path"], str):
            raise ValueError("invalid application")
        ZoneInfo(state["timezone_requested"])
        ZoneInfo(state["system_timezone"])
        return state
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PROTOTYPE.PrototypeError("没有可核验的启动记录。请完整退出 Codex 后，通过本工具重新启动；目前不能认定切换成功。") from exc


def _verify_session(state: dict[str, object], app_path: str, timeout: float, expected: str | None = None) -> dict[str, object]:
    pid, port = state["pid"], state["port"]
    command = _codex_processes(app_path).get(pid, "")
    if state.get("app_path") != app_path or f"--remote-debugging-port={port}" not in command.split():
        raise PROTOTYPE.PrototypeError("启动记录与当前 Codex 实例不匹配，无法验证；请完整退出后通过本工具启动。")
    PROTOTYPE._wait_cdp(port, timeout, url_prefix="app://")
    targets = PROTOTYPE._read_cdp_json(port, "/json/list", timeout)
    pages = []
    timezone = expected or state["timezone_requested"]
    for target in targets:
        if target.get("type") != "page" or not target.get("url", "").startswith("app://"):
            continue
        address = target.get("webSocketDebuggerUrl", "")
        if not address.startswith(f"ws://127.0.0.1:{port}/"):
            raise PROTOTYPE.PrototypeError("页面诊断地址不是预期的本机地址，未读取。")
        ws = PROTOTYPE._WebSocket(address, timeout=timeout)
        try:
            snapshot = PROTOTYPE._page_snapshot(ws)
        finally:
            ws.close()
        pages.append({"snapshot": snapshot, "checks": PROTOTYPE._check_snapshot(snapshot, timezone)})
    if not pages:
        raise PROTOTYPE.PrototypeError("当前没有可读取的 Codex 页面，无法验证；请打开 Codex 窗口后重试。")
    system_now = _system_timezone()
    checks = {
        "all_pages": all(all(page["checks"].values()) for page in pages),
        "system_timezone_unchanged": system_now == state["system_timezone"],
    }
    return {
        "status": "VERIFIED" if all(checks.values()) else "MISMATCH",
        "pid": pid,
        "mode": state["mode"],
        "timezone_requested": timezone,
        "actual_timezones": sorted({page["snapshot"]["timezone"] for page in pages}),
        "system_timezone_before": state["system_timezone"],
        "system_timezone_now": system_now,
        "pages": pages,
        "checks": checks,
        "verification_scope": "codex_page",
        "model_context_verified": False,
    }


def _report_message(report: dict[str, object]) -> str:
    if report["status"] == "VERIFIED":
        return "恢复成功：Codex 页面已采用本机时区。" if report["mode"] == "system" else "切换成功：Codex 页面时区、当地日期和 UTC 偏移均已核验。"
    return "核验未通过：Codex 页面时区或本机时区与预期不一致。"


def _emit(value: dict[str, object], as_json: bool, gui: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    lines = [str(value["message"])]
    if "egress" in value:
        egress = value["egress"]
        lines.append(f"出口：{egress['ip']} / {egress['city']} / {egress['timezone']}")
    if "timezone_requested" in value:
        lines.append(f"目标时区：{value['timezone_requested']}")
    if "actual_timezones" in value:
        lines.append("Codex 实际时区：" + "、".join(value["actual_timezones"]))
    if "system_timezone_now" in value:
        lines.append(f"本机系统时区：{value['system_timezone_now']}")
    if value.get("verification_scope") == "codex_page":
        lines.append("此报告验证应用页面；模型收到的时区需在新一轮消息中核对。")
    if value.get("guidance"):
        lines.append(str(value["guidance"]))
    message = "\n\n".join(lines)
    print(message)
    if gui:
        # 文本经 argv 传递，避免把网络响应拼接成 AppleScript 代码。
        script = '''on run argv
display dialog (item 1 of argv) with title "Scarf-Codex-Timezone" buttons {"好"} default button "好"
end run'''
        subprocess.run(["/usr/bin/osascript", "-e", script, message], capture_output=True, check=False)


def _launch_codex(app_path: str, timezone: str | None, port: int | None = None) -> int:
    app = Path(app_path)
    if not app.is_file() or not os.access(app, os.X_OK):
        raise PROTOTYPE.PrototypeError(f"找不到可执行的 Codex 应用：{app}")
    env = os.environ.copy()
    if timezone is None:
        env.pop("TZ", None)
    else:
        env["TZ"] = timezone
    command = [str(app)]
    if port is not None:
        # 仅为只读检查实际页面启用本机诊断，不设置页面覆盖或主进程调试。
        command.extend([f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"])
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    try:
        code = process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return process.pid
    raise PROTOTYPE.PrototypeError(f"Codex 启动进程已退出（{code}），尚不能确认时区切换成功。")


def _status(args: argparse.Namespace) -> dict[str, object]:
    if not _codex_running(args.codex_app):
        return {"status": "NOT_RUNNING", "message": "Codex 已完整退出，本次进程时区设置已结束。下次从原来的 Codex 图标打开，即采用正常启动环境。", "system_timezone_now": _system_timezone()}
    state = _read_state(args.state_file)
    expected = None
    egress = route = None
    route_error = None
    if state["mode"] == "egress":
        try:
            egress, route = PROTOTYPE.detect_codex_egress(args.mihomo_socket, args.codex_group, args.proxy, args.timeout, PROTOTYPE.DEFAULT_GEO_ENDPOINTS)
            _require_allowed_country(egress, args)
            expected = egress.timezone
        except (PROTOTYPE.PrototypeError, OSError, subprocess.SubprocessError) as exc:
            route_error = str(exc)
    else:
        expected = _system_timezone()
    report = _verify_session(state, args.codex_app, args.timeout, expected)
    report["message"] = _report_message(report)
    report["guidance"] = SYSTEM_GUIDE if state["mode"] == "system" else RESTORE_GUIDE
    if egress is not None:
        report.update(egress=PROTOTYPE.asdict(egress), route=route)
    if route_error:
        report["page_verification_status"] = report["status"]
        report["status"] = "UNVERIFIED"
        report["route_error"] = route_error
        report["message"] = f"已读取 Codex 页面，但无法确认当前网络出口，尚不能认定与出口同步：{route_error}"
    return report


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except PROTOTYPE.PrototypeError as exc:
        _emit({"status": "BLOCKED", "message": str(exc)}, "--json" in argv, "--gui" in argv)
        return 2
    try:
        if args.gui and not args.json and not (args.check_only or args.status or args.restore):
            action = _choose_action()
            if action is None:
                return 0
            args.status = action == "status"
            args.restore = action == "restore"
        if args.status:
            try:
                result = _status(args)
            except (PROTOTYPE.PrototypeError, OSError, subprocess.SubprocessError) as exc:
                result = {"status": "UNVERIFIED", "message": str(exc), "guidance": RESTORE_GUIDE}
            _emit(result, args.json, args.gui)
            return 0 if result["status"] in ("VERIFIED", "NOT_RUNNING") else 3
        if not args.check_only and _codex_running(args.codex_app):
            _emit({"status": "BLOCKED", "message": "操作尚未执行：Codex 正在运行。请先保存工作，在 Codex 中按 ⌘Q 完整退出，再打开本工具选择「恢复本机时区」或「按出口启动」。仅关闭窗口不等于退出。"}, args.json, args.gui)
            return 2
        system_timezone = _system_timezone()
        result = {}
        if args.restore:
            target, mode = system_timezone, "system"
        else:
            egress, route = PROTOTYPE.detect_codex_egress(
                args.mihomo_socket, args.codex_group, args.proxy, args.timeout, PROTOTYPE.DEFAULT_GEO_ENDPOINTS,
            )
            result.update(egress=PROTOTYPE.asdict(egress), route=route)
            _require_allowed_country(egress, args)
            target, mode = egress.timezone, "egress"
        if args.check_only:
            result.update(status="CHECKED", message="仅检测出口，未启动 Codex；这不代表当前实例已切换。")
            _emit(result, args.json, args.gui)
            return 0
        if _codex_running(args.codex_app):
            result["status"] = "BLOCKED"
            result["message"] = "检测到 ChatGPT/Codex 正在运行；为避免影响当前对话，未强制关闭。请退出后再次点击。"
            _emit(result, args.json, args.gui)
            return 2
        if mode == "egress":
            # 出口检测与启动间可能有手动切换；启动前再读取一次活动选择。
            chain, leaf = PROTOTYPE.resolve_mihomo_chain(args.mihomo_socket, args.codex_group, args.timeout)
            if chain != route["chain"] or leaf != route["leaf"]:
                raise PROTOTYPE.PrototypeError("检测后 Codex/Magpie 出口发生变化，未启动；请重新点击。")
            PROTOTYPE.verify_codex_route(args.mihomo_socket, args.codex_group, leaf, args.timeout)
        port = PROTOTYPE._find_free_port()
        pid = _launch_codex(args.codex_app, None if args.restore else target, port)
        state = {"pid": pid, "port": port, "app_path": args.codex_app, "mode": mode, "timezone_requested": target, "system_timezone": system_timezone}
        result.update(pid=pid, timezone_requested=target, guidance=SYSTEM_GUIDE if args.restore else RESTORE_GUIDE)
        try:
            _write_state(args.state_file, state)
            result.update(_verify_session(state, args.codex_app, args.timeout))
            result["message"] = _report_message(result)
        except (PROTOTYPE.PrototypeError, OSError, subprocess.SubprocessError) as exc:
            result.update(status="UNVERIFIED", message=f"Codex 已启动，但尚未验证时区，不能报告成功：{exc}\n请稍后打开本工具，选择「检查是否成功」。")
        _emit(result, args.json, args.gui)
        return 0 if result["status"] == "VERIFIED" else 3
    except (PROTOTYPE.PrototypeError, OSError, subprocess.SubprocessError) as exc:
        value = {"status": "BLOCKED", "message": str(exc)}
        _emit(value, args.json, args.gui)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
