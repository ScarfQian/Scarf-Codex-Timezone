#!/usr/bin/env python3
"""已确认缺陷的本地回归检查。日期：2026-10-15；创建人：ScarfQian。"""

import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import struct
import sys
import threading
import tempfile
import plistlib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.parse import unquote


sys.dont_write_bytecode = True
MODULE_PATH = Path(__file__).with_name("timezone_probe.py")
SPEC = importlib.util.spec_from_file_location("timezone_prototype", MODULE_PATH)
PROTOTYPE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROTOTYPE
SPEC.loader.exec_module(PROTOTYPE)
LAUNCHER_SPEC = importlib.util.spec_from_file_location("timezone_launcher", MODULE_PATH.with_name("launcher.py"))
LAUNCHER = importlib.util.module_from_spec(LAUNCHER_SPEC)
LAUNCHER_SPEC.loader.exec_module(LAUNCHER)
LAUNCHER.PROTOTYPE = PROTOTYPE

# 本文件的 IP/地理字段仅为合成 fixture，不查询网络，也不表示示例 IP 的真实位置。


class FragmentedSocket:
    """按一个字节交付合法 TCP 流，覆盖帧头与扩展长度拆分。"""

    def __init__(self, data):
        self.data = bytearray(data)

    def recv(self, count):
        size = min(count, 1, len(self.data))
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk


class PrototypeRegressionTests(unittest.TestCase):
    def test_websocket_split_headers_and_payload(self):
        for size in (2, 126, 65536):
            with self.subTest(payload_size=size):
                payload = b"x" * size
                if size < 126:
                    header = bytes((0x81, size))
                elif size < 65536:
                    header = b"\x81\x7e" + struct.pack("!H", size)
                else:
                    header = b"\x81\x7f" + struct.pack("!Q", size)
                ws = PROTOTYPE._WebSocket.__new__(PROTOTYPE._WebSocket)
                ws.sock = FragmentedSocket(header + payload)
                ws._buffer = bytearray()
                self.assertEqual(ws._recv_frame(), (1, payload))

    def test_handshake_keeps_following_frame_bytes(self):
        ws = PROTOTYPE._WebSocket.__new__(PROTOTYPE._WebSocket)
        ws.sock = FragmentedSocket(b"")
        ws._buffer = bytearray(b"HTTP/1.1 101 OK\r\n\r\n\x81\x02{}")
        self.assertEqual(ws._read_until(b"\r\n\r\n"), b"HTTP/1.1 101 OK\r\n\r\n")
        self.assertEqual(ws._recv_frame(), (1, b"{}"))

    def test_dates_offsets_and_wrong_readings(self):
        # UTC 2026-01-01 00:30 与 2026-07-01 00:30，洛杉矶均为前一天。
        for timestamp, day, offset in (
            (1767227400000, "2025-12-31", 480),
            (1782865800000, "2026-06-30", 420),
        ):
            snapshot = {"timestampMs": timestamp, "timezone": "America/Los_Angeles", "localDate": day, "offsetMinutes": offset}
            self.assertTrue(all(PROTOTYPE._check_snapshot(snapshot, "America/Los_Angeles").values()))
            snapshot["offsetMinutes"] = -480
            self.assertFalse(PROTOTYPE._check_snapshot(snapshot, "America/Los_Angeles")["utc_offset"])

    def test_public_egress_fields(self):
        for update in ({"ip": "127.0.0.1"}, {"timezone": "wrong type"}, {"timezone": {"id": "../UTC"}}):
            with self.subTest(update=update):
                data = {"ip": "8.8.8.8", "country_code": "US", "timezone": {"id": "America/Los_Angeles"}}
                data.update(update)
                with self.assertRaises(PROTOTYPE.PrototypeError):
                    PROTOTYPE._normalize_geo(data, "https://ipwho.is/")

    def test_outbound_proxy_and_local_direct_are_separate(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                body = json.dumps({"ip": "8.8.8.8", "country": "US", "timezone": "America/Los_Angeles"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proxy = f"http://127.0.0.1:{server.server_port}"
        try:
            # 即使 no_proxy=*，外部查询仍须走显式代理；invalid 域名不会被真实解析。
            with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
                value = PROTOTYPE._json_request("http://example.invalid/egress", proxy, 2)
            self.assertEqual(value["country"], "US")
            self.assertEqual(requests, ["http://example.invalid/egress"])
            # 反过来，本地 CDP 必须忽略环境代理，直接抵达本地 fixture。
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}):
                PROTOTYPE._read_cdp_json(server.server_port, "/json/version", 2)
            self.assertEqual(requests[-1], "/json/version")
            with self.assertRaises(PROTOTYPE.PrototypeError):
                PROTOTYPE._json_request("http://example.invalid/egress", "http://127.0.0.1:1", 0.3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_geolocation_disagreement_is_rejected(self):
        responses = iter(
            [
                {"ip": "1.1.1.1", "country_code": "US", "timezone": {"id": "America/New_York"}},
                {"ip": "8.8.8.8", "country": "US", "timezone": "America/Los_Angeles"},
            ]
        )
        with patch.object(PROTOTYPE, "_json_request", side_effect=lambda *args: next(responses)):
            with self.assertRaisesRegex(PROTOTYPE.PrototypeError, "不一致"):
                PROTOTYPE.detect_egress("http://127.0.0.1:7897", 1, PROTOTYPE.DEFAULT_GEO_ENDPOINTS)

    def test_mihomo_chain_resolves_groups_to_leaf(self):
        values = {
            "GPT": {"type": "Selector", "now": "Model-Route"},
            "Model-Route": {"type": "Selector", "now": "Example-Selection"},
            "Example-Selection": {"type": "Fallback", "now": "Example-US-Node"},
            "Example-US-Node": {"type": "Vless"},
        }
        with patch.object(PROTOTYPE, "_mihomo_json", side_effect=lambda _socket, path, _timeout: values[unquote(path.rsplit('/', 1)[-1])]):
            self.assertEqual(
                PROTOTYPE.resolve_mihomo_chain("/tmp/mihomo.sock", "GPT", 1),
                (["GPT", "Model-Route", "Example-Selection", "Example-US-Node"], "Example-US-Node"),
            )

    def test_chatgpt_trace_requires_the_expected_host_and_public_ip(self):
        with patch.object(PROTOTYPE, "_proxy_request", return_value=b"h=chatgpt.com\nip=9.9.9.9\nloc=US\n"):
            self.assertEqual(PROTOTYPE._read_chatgpt_ip("http://127.0.0.1:7897", 1), "9.9.9.9")
        for response in (b"<html>blocked</html>", b"h=other.example\nip=9.9.9.9", b"h=chatgpt.com\nip=127.0.0.1"):
            with self.subTest(response=response), patch.object(PROTOTYPE, "_proxy_request", return_value=response):
                with self.assertRaises(PROTOTYPE.PrototypeError):
                    PROTOTYPE._read_chatgpt_ip("http://127.0.0.1:7897", 1)

    def test_geolocation_queries_trace_ip_instead_of_query_service_egress(self):
        expected = "9.9.9.9"
        responses = [
            {"ip": expected, "country_code": "US", "timezone": {"id": "America/New_York"}},
            {"ip": expected, "country": "US", "timezone": "America/New_York"},
        ]
        with patch.object(PROTOTYPE, "_json_request", side_effect=responses) as request:
            result = PROTOTYPE.detect_egress("http://127.0.0.1:7897", 1, PROTOTYPE.DEFAULT_GEO_ENDPOINTS, expected_ip=expected)
            self.assertEqual(result.ip, expected)
            self.assertEqual([call.args[0] for call in request.call_args_list], [f"https://ipwho.is/{expected}", f"https://ipinfo.io/{expected}/json"])
            self.assertEqual(len(result.verified_endpoints), 2)
        wrong = {"ip": "8.8.8.8", "country_code": "US", "timezone": {"id": "America/Los_Angeles"}}
        with patch.object(PROTOTYPE, "_json_request", return_value=wrong):
            with self.assertRaises(PROTOTYPE.PrototypeError):
                PROTOTYPE.detect_egress("http://127.0.0.1:7897", 1, ("https://ipwho.is/",), expected_ip=expected)

    def test_load_balancing_cannot_be_misrepresented_by_one_leaf(self):
        for kind in ("LoadBalance", "Relay"):
            with self.subTest(kind=kind), patch.object(PROTOTYPE, "_mihomo_json", return_value={"type": kind, "now": "node"}):
                with self.assertRaises(PROTOTYPE.PrototypeError):
                    PROTOTYPE.resolve_mihomo_chain("/tmp/mihomo.sock", "group", 1)

    def test_changed_rules_or_existing_route_are_rejected(self):
        rules = {"rules": [{"type": "DomainSuffix", "payload": domain, "proxy": "GPT"} for domain in ("openai.com", "chatgpt.com")]}
        with patch.object(PROTOTYPE, "_mihomo_json", side_effect=[{"mode": "rule"}, rules, {"connections": []}]):
            PROTOTYPE.verify_codex_route("/tmp/mihomo.sock", "GPT", "node", 1)
        with patch.object(PROTOTYPE, "_mihomo_json", return_value={"mode": "global"}):
            with self.assertRaises(PROTOTYPE.PrototypeError):
                PROTOTYPE.verify_codex_route("/tmp/mihomo.sock", "GPT", "node", 1)
        with patch.object(PROTOTYPE, "_mihomo_json", side_effect=[{"mode": "rule"}, {"rules": []}]):
            with self.assertRaises(PROTOTYPE.PrototypeError):
                PROTOTYPE.verify_codex_route("/tmp/mihomo.sock", "GPT", "node", 1)
        old = {"connections": [{"metadata": {"process": "magpie", "host": "chatgpt.com"}, "chains": ["old-node"]}]}
        with patch.object(PROTOTYPE, "_mihomo_json", side_effect=[{"mode": "rule"}, rules, old]):
            with self.assertRaises(PROTOTYPE.PrototypeError):
                PROTOTYPE.verify_codex_route("/tmp/mihomo.sock", "GPT", "node", 1)

    def test_running_app_and_ps_failure_are_distinguished(self):
        app = PROTOTYPE.DEFAULT_CODEX_APP
        for code, output, expected in ((0, f"123 {app}\n", True), (0, "123 /usr/bin/other\n", False)):
            completed = subprocess.CompletedProcess([], code, stdout=output)
            with patch.object(LAUNCHER.subprocess, "run", return_value=completed):
                self.assertEqual(LAUNCHER._codex_running(app), expected)
        with patch.object(LAUNCHER.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="")):
            with self.assertRaises(PROTOTYPE.PrototypeError):
                LAUNCHER._codex_running(app)

    def test_existing_app_stops_before_network_or_launch(self):
        with patch.object(LAUNCHER, "_codex_running", return_value=True), patch.object(PROTOTYPE, "detect_codex_egress") as detect, patch.object(LAUNCHER, "_launch_codex") as launch:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(LAUNCHER.main(["--json"]), 2)
            detect.assert_not_called()
            launch.assert_not_called()

    def test_immediate_exit_is_not_reported_as_launch_success(self):
        process = Mock(pid=321)
        process.wait.return_value = 0
        with patch.object(LAUNCHER.subprocess, "Popen", return_value=process):
            with self.assertRaises(PROTOTYPE.PrototypeError):
                LAUNCHER._launch_codex(sys.executable, "America/New_York")
        process.wait.side_effect = subprocess.TimeoutExpired("Codex", 2)
        with patch.object(LAUNCHER.subprocess, "Popen", return_value=process) as start:
            self.assertEqual(LAUNCHER._launch_codex(sys.executable, "America/New_York"), 321)
            self.assertEqual(start.call_args.kwargs["env"]["TZ"], "America/New_York")


class LauncherStatusTests(unittest.TestCase):
    def setUp(self):
        self.state = {"pid": 321, "port": 43210, "app_path": PROTOTYPE.DEFAULT_CODEX_APP, "mode": "egress", "timezone_requested": "America/New_York", "system_timezone": "Asia/Shanghai"}
        self.snapshot = {"timestampMs": 1767227400000, "timezone": "America/New_York", "localDate": "2025-12-31", "offsetMinutes": 300}

    def verify_pages(self, snapshots, system="Asia/Shanghai", command=None):
        command = command or f"{PROTOTYPE.DEFAULT_CODEX_APP} --remote-debugging-port=43210"
        targets = [{"type": "page", "url": "app://codex/", "webSocketDebuggerUrl": f"ws://127.0.0.1:43210/devtools/page/{index}"} for index in range(len(snapshots))]
        ws = Mock()
        ws.call.side_effect = [{"result": {"value": snapshot}} for snapshot in snapshots]
        with patch.object(LAUNCHER, "_codex_processes", return_value={321: command}), patch.object(LAUNCHER, "_system_timezone", return_value=system), patch.object(PROTOTYPE, "_wait_cdp"), patch.object(PROTOTYPE, "_read_cdp_json", return_value=targets), patch.object(PROTOTYPE, "_WebSocket", return_value=ws):
            report = LAUNCHER._verify_session(self.state, PROTOTYPE.DEFAULT_CODEX_APP, 1)
        return report, ws

    def test_verified_report_reads_actual_pages_without_overriding_them(self):
        report, ws = self.verify_pages([self.snapshot, self.snapshot])
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["actual_timezones"], ["America/New_York"])
        self.assertFalse(report["model_context_verified"])
        self.assertEqual([call.args[0] for call in ws.call.call_args_list], ["Runtime.evaluate", "Runtime.evaluate"])
        self.assertEqual(ws.close.call_count, 2)

    def test_wrong_page_or_system_timezone_cannot_be_reported_as_success(self):
        wrong = dict(self.snapshot, timezone="UTC", localDate="2026-01-01", offsetMinutes=0)
        for snapshots, system in (([self.snapshot, wrong], "Asia/Shanghai"), ([self.snapshot], "UTC")):
            with self.subTest(snapshots=snapshots, system=system):
                report, _ = self.verify_pages(snapshots, system)
                self.assertEqual(report["status"], "MISMATCH")

    def test_stale_record_is_rejected_before_reading_a_different_instance(self):
        with patch.object(LAUNCHER, "_codex_processes", return_value={321: PROTOTYPE.DEFAULT_CODEX_APP}), patch.object(PROTOTYPE, "_wait_cdp") as read:
            with self.assertRaises(PROTOTYPE.PrototypeError):
                LAUNCHER._verify_session(self.state, PROTOTYPE.DEFAULT_CODEX_APP, 1)
            read.assert_not_called()

    def test_restore_clears_inherited_tz_without_changing_parent(self):
        process = Mock(pid=321)
        process.wait.side_effect = subprocess.TimeoutExpired("Codex", 2)
        with patch.dict(os.environ, {"TZ": "America/New_York"}), patch.object(LAUNCHER.subprocess, "Popen", return_value=process) as start:
            self.assertEqual(LAUNCHER._launch_codex(sys.executable, None, 43210), 321)
            self.assertNotIn("TZ", start.call_args.kwargs["env"])
            self.assertEqual(os.environ["TZ"], "America/New_York")
            self.assertNotIn("--remote-allow-origins=*", start.call_args.args[0])

    def test_restore_needs_no_clash_or_network(self):
        report = {"status": "VERIFIED", "mode": "system", "actual_timezones": ["Asia/Shanghai"]}
        with tempfile.TemporaryDirectory(prefix="scarf-timezone-test-") as directory:
            state_path = Path(directory) / "session.json"
            with patch.object(LAUNCHER, "_codex_running", return_value=False), patch.object(LAUNCHER, "_system_timezone", return_value="Asia/Shanghai"), patch.object(LAUNCHER, "_launch_codex", return_value=321) as launch, patch.object(LAUNCHER, "_verify_session", return_value=report), patch.object(PROTOTYPE, "_find_free_port", return_value=43210), patch.object(PROTOTYPE, "detect_codex_egress") as network:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(LAUNCHER.main(["--restore", "--json", "--state-file", str(state_path)]), 0)
                self.assertIsNone(launch.call_args.args[1])
                self.assertEqual(LAUNCHER._read_state(state_path)["timezone_requested"], "Asia/Shanghai")
                network.assert_not_called()

    def test_restore_does_not_close_or_replace_running_codex(self):
        with patch.object(LAUNCHER, "_codex_running", return_value=True), patch.object(LAUNCHER, "_launch_codex") as launch, patch.object(PROTOTYPE, "detect_codex_egress") as network:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(LAUNCHER.main(["--restore", "--json"]), 2)
            launch.assert_not_called()
            network.assert_not_called()

    def test_network_failure_does_not_turn_page_only_result_into_overall_success(self):
        report = {"status": "VERIFIED", "mode": "egress"}
        args = LAUNCHER.parse_args(["--status"])
        with patch.object(LAUNCHER, "_codex_running", return_value=True), patch.object(LAUNCHER, "_read_state", return_value=self.state), patch.object(LAUNCHER, "_verify_session", return_value=report), patch.object(PROTOTYPE, "detect_codex_egress", side_effect=PROTOTYPE.PrototypeError("offline")):
            actual = LAUNCHER._status(args)
        self.assertEqual(actual["status"], "UNVERIFIED")
        self.assertEqual(actual["page_verification_status"], "VERIFIED")

    def test_status_uses_current_exit_timezone_instead_of_old_launch_target(self):
        args = LAUNCHER.parse_args(["--status"])
        current = PROTOTYPE.EgressInfo("8.8.8.8", "US", "United States", "Los Angeles", "America/Los_Angeles", "diagnostic")
        with patch.object(LAUNCHER, "_codex_running", return_value=True), patch.object(LAUNCHER, "_read_state", return_value=self.state), patch.object(LAUNCHER, "_verify_session", return_value={"status": "MISMATCH", "mode": "egress"}) as verify, patch.object(PROTOTYPE, "detect_codex_egress", return_value=(current, {})):
            report = LAUNCHER._status(args)
        self.assertEqual(verify.call_args.args[3], "America/Los_Angeles")
        self.assertEqual(report["status"], "MISMATCH")

    def test_closed_codex_cannot_reuse_a_previous_success_record(self):
        with patch.object(LAUNCHER, "_codex_running", return_value=False), patch.object(LAUNCHER, "_system_timezone", return_value="Asia/Shanghai"), patch.object(LAUNCHER, "_read_state") as record:
            report = LAUNCHER._status(LAUNCHER.parse_args(["--status"]))
        self.assertEqual(report["status"], "NOT_RUNNING")
        record.assert_not_called()

    def test_cancelling_tool_does_not_start_or_change_codex(self):
        with patch.object(LAUNCHER, "_choose_action", return_value=None), patch.object(LAUNCHER, "_launch_codex") as launch, patch.object(PROTOTYPE, "detect_codex_egress") as network:
            self.assertEqual(LAUNCHER.main(["--gui"]), 0)
        launch.assert_not_called()
        network.assert_not_called()


class PublicationConfigurationTests(unittest.TestCase):
    def test_json_settings_and_cli_override_are_applied(self):
        with tempfile.TemporaryDirectory(prefix="scarf-config-test-") as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"proxy": "http://127.0.0.1:7890", "codex_group": "Example-Group", "allowed_countries": ["US", "CA"], "timeout": 5}), encoding="utf-8")
            args = LAUNCHER.parse_args(["--config", str(path), "--proxy", "http://127.0.0.1:7891"])
        self.assertEqual(args.proxy, "http://127.0.0.1:7891")
        self.assertEqual(args.codex_group, "Example-Group")
        self.assertEqual(args.allowed_countries, {"US", "CA"})
        self.assertEqual(args.timeout, 5)

    def test_country_policy_is_configurable_and_enforced(self):
        egress = PROTOTYPE.EgressInfo("8.8.8.8", "JP", "Japan", "Example", "Asia/Tokyo", "fixture")
        with self.assertRaises(PROTOTYPE.PrototypeError):
            LAUNCHER._require_allowed_country(egress, LAUNCHER.parse_args([]))
        for countries in ("US,JP", "*"):
            LAUNCHER._require_allowed_country(egress, LAUNCHER.parse_args(["--countries", countries]))

    def test_invalid_or_missing_explicit_configuration_is_not_ignored(self):
        with tempfile.TemporaryDirectory(prefix="scarf-config-test-") as directory:
            path = Path(directory) / "config.json"
            for config in ([], {"proxy": 7897}, {"allowed_countries": "US"}, {"timeout": -1}, {"unknown": True}):
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(config=config), self.assertRaises(PROTOTYPE.PrototypeError):
                    LAUNCHER.parse_args(["--config", str(path)])
            path.unlink()
            with self.assertRaises(PROTOTYPE.PrototypeError):
                LAUNCHER.parse_args(["--config", str(path)])

    def test_config_errors_are_reported_without_starting_codex(self):
        with patch.object(LAUNCHER, "parse_args", side_effect=PROTOTYPE.PrototypeError("invalid config")), patch.object(LAUNCHER, "_launch_codex") as launch:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(LAUNCHER.main(["--json"]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "BLOCKED")
        launch.assert_not_called()

    def test_built_app_contains_only_public_resources_and_uses_selected_python(self):
        import build_app

        with tempfile.TemporaryDirectory(prefix="scarf-build-test-") as directory:
            app = build_app.build_app(Path(directory) / "Scarf-Test.app", Path(sys.executable))
            with (app / "Contents/Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            self.assertEqual(info["CFBundleDisplayName"], "Scarf-Codex-Timezone")
            self.assertEqual({path.name for path in (app / "Contents/Resources").iterdir()}, {"launcher.py", "timezone_probe.py", "README.md", "LICENSE", "config.example.json"})
            entry = app / "Contents/MacOS/Scarf-Timezone"
            self.assertTrue(os.access(entry, os.X_OK))
            self.assertIn(str(Path(sys.executable).resolve()), entry.read_text())
            with self.assertRaises(ValueError):
                build_app.build_app(app, Path(sys.executable))


if __name__ == "__main__":
    unittest.main()
