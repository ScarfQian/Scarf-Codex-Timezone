"""持久自动模式、默认启动入口与只读后台核验。日期：2026-10-16；创建人：ScarfQian。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import time


AGENT_LABEL = "io.github.scarfqian.codex-timezone.auto"
ENTRY_ID = "io.github.scarfqian.codex-timezone.launch"
POLL_SECONDS = 15
TARGET_MAX_AGE = 120
RUNTIME_FILES = ("launcher.py", "timezone_probe.py", "auto_mode.py")


def read_mode(folder: Path) -> str:
    path = folder / "preferences.json"
    if not path.exists():
        # 首次使用默认开启；显式保存的关闭设置始终优先。
        return "auto"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("mode") not in ("auto", "manual"):
        raise ValueError("模式设置无效，请重新选择自动或手动。")
    return value["mode"]


def _cached(folder: Path) -> dict:
    path = folder / "auto-status.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _service(verb: str, *arguments: str, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(["/bin/launchctl", verb, *arguments], capture_output=True, text=True, timeout=10)
    if check and result.returncode:
        raise RuntimeError(f"后台服务操作未完成：{result.stderr.strip() or result.stdout.strip()}")
    return result


def service_loaded() -> bool:
    return _service("print", f"gui/{os.getuid()}/{AGENT_LABEL}").returncode == 0


def details(folder: Path) -> dict:
    mode = read_mode(folder)
    result = {"automation_mode": mode, "service_loaded": service_loaded(), "status": "MODE_STATUS"}
    cached = _cached(folder)
    if mode == "auto":
        result["auto_status"] = cached
        result["message"] = "自动跟随已开启。" if result["service_loaded"] else "自动跟随默认开启；请打开本工具完成后台设置。"
        if cached:
            stale = time.time() - cached.get("checked_at", 0) > POLL_SECONDS * 3
            result["status_stale"] = stale
            result["message"] += "\n" + ("等待新一轮核验，上次记录不代表当前状态。" if stale else cached.get("message", ""))
            result["last_checked_at"] = cached.get("checked_at")
    else:
        result["message"] = "自动跟随已关闭，此设置会保留到你再次点击「开启自动」。当前 Codex 时区保持原样。"
    return result


def _dock_read() -> dict:
    result = subprocess.run(["/usr/bin/defaults", "export", "com.apple.dock", "-"], capture_output=True, check=True, timeout=10)
    return plistlib.loads(result.stdout)


def _dock_write(tiles: list[dict]) -> None:
    # 只写 persistent-apps，不导入或覆盖其他 Dock 偏好。
    values = [plistlib.dumps(tile, fmt=plistlib.FMT_XML).decode("utf-8") for tile in tiles]
    subprocess.run(["/usr/bin/defaults", "write", "com.apple.dock", "persistent-apps", "-array", *values], capture_output=True, check=True, timeout=10)
    actual = _dock_read().get("persistent-apps", [])
    expected_urls = [tile.get("tile-data", {}).get("file-data", {}).get("_CFURLString") for tile in tiles]
    actual_urls = [tile.get("tile-data", {}).get("file-data", {}).get("_CFURLString") for tile in actual]
    if actual_urls != expected_urls:
        raise RuntimeError("Dock 入口写入后核对不一致，未确认设置成功。")
    subprocess.run(["/usr/bin/killall", "Dock"], capture_output=True, check=False, timeout=5)


def _dock_target(tile: dict) -> str:
    return tile.get("tile-data", {}).get("bundle-identifier", "")


def dock_enable(folder: Path, entry: Path, app_path: str) -> bool:
    bundle = Path(app_path).parents[2]
    with (bundle / "Contents/Info.plist").open("rb") as stream:
        app_id = plistlib.load(stream).get("CFBundleIdentifier")
    if not app_id:
        raise ValueError("无法确认 Codex 的应用标识，未修改 Dock。")
    tiles = _dock_read().get("persistent-apps", [])
    if any(_dock_target(tile) == ENTRY_ID for tile in tiles):
        return False
    index = next((index for index, tile in enumerate(tiles) if _dock_target(tile) == app_id), None)
    backup_path = folder / "dock-entry-backup.plist"
    if backup_path.exists():
        raise ValueError("存在尚未恢复的 Dock 入口备份，请先切回手动后重试，避免覆盖已有修改。")
    # 只保存我们替换的一个入口；切回手动时保留用户对其他入口的调整。
    backup = {"had_original": index is not None, "original": tiles[index] if index is not None else {}}
    backup_path.write_bytes(plistlib.dumps(backup))
    tile = {"tile-type": "file-tile", "tile-data": {"file-data": {"_CFURLString": entry.as_uri() + "/", "_CFURLStringType": 15}, "file-label": "Codex（时区启动）", "bundle-identifier": ENTRY_ID, "file-type": 41}}
    if index is None:
        tiles.append(tile)
    else:
        tiles[index] = tile
    _dock_write(tiles)
    return True


def dock_restore(folder: Path) -> bool:
    backup_path = folder / "dock-entry-backup.plist"
    if not backup_path.exists():
        return False
    backup = plistlib.loads(backup_path.read_bytes())
    tiles = _dock_read().get("persistent-apps", [])
    index = next((index for index, tile in enumerate(tiles) if _dock_target(tile) == ENTRY_ID), None)
    if index is not None:
        if backup["had_original"]:
            tiles[index] = backup["original"]
        else:
            tiles.pop(index)
        _dock_write(tiles)
    backup_path.unlink()
    return index is not None


def _install_runtime(app, args) -> tuple[Path, Path]:
    folder = app.SETTINGS_DIR
    runtime = folder / "runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in RUNTIME_FILES:
        source, destination = app.ROOT / name, runtime / name
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
    # 配置保存在用户目录，后台服务不依赖外置磁盘或桌面工具的摆放位置。
    app._write_state(folder / "config.json", {"codex_app": args.codex_app, "proxy": args.proxy, "mihomo_socket": args.mihomo_socket, "codex_group": args.codex_group, "allowed_countries": sorted(args.allowed_countries), "timeout": args.timeout})
    entry = Path.home() / "Applications/Scarf-Codex-Timezone-Launch.app"
    contents = entry / "Contents"
    existing_info = contents / "Info.plist"
    if entry.exists():
        if not existing_info.is_file() or plistlib.loads(existing_info.read_bytes()).get("CFBundleIdentifier") != ENTRY_ID:
            raise ValueError("同名启动入口不属于本工具，未覆盖。")
    macos = contents / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    info = {"CFBundleName": "Codex（时区启动）", "CFBundleDisplayName": "Codex（时区启动）", "CFBundleIdentifier": ENTRY_ID, "CFBundleExecutable": "Scarf-Timezone-Launch", "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.2.1", "CFBundleVersion": "3", "LSUIElement": True, "LSMultipleInstancesProhibited": True}
    existing_info.write_bytes(plistlib.dumps(info))
    script = macos / "Scarf-Timezone-Launch"
    script.write_text("#!/bin/zsh\nset -euo pipefail\nexec " + shlex.quote(str(Path(sys.executable).resolve())) + " -B " + shlex.quote(str(runtime / "launcher.py")) + ' --default-open "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    return runtime, entry


def set_mode(app, args, mode: str) -> dict:
    folder = app.SETTINGS_DIR
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    preference = folder / "preferences.json"
    agent_path = Path.home() / "Library/LaunchAgents" / (AGENT_LABEL + ".plist")
    if mode == "manual":
        app._write_state(preference, {"mode": "manual"})
        if service_loaded():
            _service("bootout", f"gui/{os.getuid()}/{AGENT_LABEL}")
            if service_loaded():
                raise RuntimeError("已保存手动模式，但后台服务尚未卸载，请再次选择手动；服务不会再执行自动检测。")
        agent_path.unlink(missing_ok=True)
        restored = dock_restore(folder)
        return {"status": "MODE_CHANGED", "automation_mode": "manual", "service_loaded": False, "dock_restored": restored, "message": "自动跟随已关闭，后台检测已停止，原 Dock 入口已恢复。此设置会保留到你再次点击「开启自动」。当前 Codex 的时区保持原样。"}
    if read_mode(folder) == "auto" and service_loaded():
        return details(folder)
    runtime, entry = _install_runtime(app, args)
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent = {"Label": AGENT_LABEL, "ProgramArguments": [str(Path(sys.executable).resolve()), "-B", str(runtime / "launcher.py"), "--auto-tick", "--json"], "RunAtLoad": True, "StartInterval": POLL_SECONDS, "ProcessType": "Background", "StandardOutPath": "/dev/null", "StandardErrorPath": str(folder / "auto-service.log")}
    agent_path.write_bytes(plistlib.dumps(agent))
    app._write_state(preference, {"mode": "auto"})
    try:
        dock_enable(folder, entry, args.codex_app)
        _service("bootstrap", f"gui/{os.getuid()}", str(agent_path), check=True)
    except Exception:
        app._write_state(preference, {"mode": "manual"})
        if service_loaded():
            _service("bootout", f"gui/{os.getuid()}/{AGENT_LABEL}")
        agent_path.unlink(missing_ok=True)
        dock_restore(folder)
        raise
    return {"status": "MODE_CHANGED", "automation_mode": "auto", "service_loaded": service_loaded(), "default_entry": str(entry), "message": "自动模式已启用并保存，关闭工具窗口不会取消。后台持续检测出口；日常请使用 Dock 的「Codex（时区启动）」入口。正在运行的 Codex 不会被强制退出；需要重启才能同步时会显示「待同步」。"}


def tick(app, args) -> dict:
    folder = app.SETTINGS_DIR
    if read_mode(folder) != "auto":
        return {"status": "MANUAL", "automation_mode": "manual", "message": "手动模式不执行自动检测。"}
    previous = _cached(folder)
    target = previous.get("target") or {}
    now = time.time()
    result = {"automation_mode": "auto", "checked_at": now, "model_context_verified": False, "verification_scope": "codex_page"}
    try:
        chain, leaf = app.PROTOTYPE.resolve_mihomo_chain(args.mihomo_socket, args.codex_group, args.timeout)
        if not target or target.get("route", {}).get("chain") != chain or now - target.get("detected_at", 0) >= TARGET_MAX_AGE:
            egress, route = app.PROTOTYPE.detect_codex_egress(args.mihomo_socket, args.codex_group, args.proxy, args.timeout, app.PROTOTYPE.DEFAULT_GEO_ENDPOINTS)
            app._require_allowed_country(egress, args)
            target = {"timezone": egress.timezone, "egress": app.PROTOTYPE.asdict(egress), "route": route, "detected_at": now}
        else:
            app.PROTOTYPE.verify_codex_route(args.mihomo_socket, args.codex_group, leaf, args.timeout)
        result.update(target=target, target_current=True, timezone_requested=target["timezone"])
        processes = app._codex_processes(args.codex_app)
        if not processes:
            result.update(status="READY", message="自动已启用，出口目标已准备好；Codex 当前未运行。下次从 Dock 时区入口打开会自动应用并核验。")
        else:
            result["pid"] = next(iter(processes))
            try:
                state = app._read_state(args.state_file)
                report = app._verify_session(state, args.codex_app, args.timeout, target["timezone"])
                base_matches = state["mode"] == "egress" and state["timezone_requested"] == target["timezone"]
                result.update(actual_timezones=report["actual_timezones"], system_timezone_now=report["system_timezone_now"], page_checks=report["checks"])
                if report["status"] == "VERIFIED" and base_matches:
                    result.update(status="VERIFIED", message="自动已启用：当前 Codex 启动时区和实际页面均与出口一致，本机系统时区未变。")
                else:
                    result.update(status="PENDING_RESTART", message="自动已启用，但当前 Codex 与出口尚未完整同步。请保存工作后按 ⌘Q 退出，再从 Dock 时区入口打开；不会强制退出。")
            except (app.PROTOTYPE.PrototypeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                result.update(status="PENDING_RESTART", message="自动已启用；当前实例无法核验，需要从 Dock 时区入口重新启动后同步。", detail=str(exc))
    except (app.PROTOTYPE.PrototypeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        result.update(status="UNVERIFIED", target=target, target_current=False, message=f"自动已启用，但当前出口暂时无法确认，未修改 Codex：{exc}")
    # 切回手动时，尚在执行的网络检查不得再覆盖自动状态。
    if read_mode(folder) != "auto":
        return {"status": "MANUAL", "automation_mode": "manual", "message": "自动检测已取消。"}
    app._write_state(folder / "auto-status.json", result)
    return result


def focus_existing(pid: int) -> None:
    # 按现有 PID 激活窗口；不调用 open，避免进程已退出时意外以正常环境重开。
    script = '''ObjC.import('AppKit');
function run(argv) {
    const app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(Number(argv[0]));
    if (app) app.activateWithOptions(2);
}'''
    subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script, str(pid)], capture_output=True, check=False, timeout=5)
