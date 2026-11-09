"""Exercise the real MCP, worker process, and lifecycle entry points against a local fake."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from websockets.sync.server import serve

from tools.usage.protocol import Client
from tools.usage.service import UsageService
from tools.usage.state import Store, process_alive
from tools.usage.lifecycle import record_event

ROOT = Path(__file__).resolve().parents[3]


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.status, self.used, self.credits, self.consumed = "active", 50, 2, 0
        self.server = serve(self.handle, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"
        self.env = {**os.environ, "CODEX_USAGE_ENDPOINT": self.endpoint,
                    "CODEX_USAGE_STATE_DIR": self.temp.name, "CODEX_THREAD_ID": "test-owner"}
        self.client = Client(self.endpoint).open()
        self.addCleanup(self.client.close)
        self.service = UsageService(self.store, self.client, "test-owner", poll_seconds=0)
        self.addCleanup(self.cleanup_worker)

    def cleanup_worker(self):
        hook = self.store.read("hook")
        if hook:
            UsageService(self.store, self.client, hook["thread_id"]).expire()

    def handle(self, websocket):
        for raw in websocket:
            message = json.loads(raw)
            if "id" not in message:
                continue
            method = message["method"]
            if method == "initialize":
                result = {"userAgent": "test"}
            elif method == "thread/loaded/list":
                result = {"data": ["test-owner", "competitor"], "nextCursor": None}
            elif method == "thread/read":
                result = {"thread": {"status": {"type": self.status}}}
            elif method == "account/rateLimits/read":
                result = {"accountId": "fake-account", "rateLimits": {"limitId": "codex",
                          "primary": {"usedPercent": self.used, "windowDurationMins": 300}},
                          "rateLimitResetCredits": {"availableCount": self.credits}}
            elif method == "account/rateLimitResetCredit/consume":
                self.consumed += 1
                self.credits -= 1
                self.used = 0
                result = {"outcome": "reset"}
            else:
                raise AssertionError(method)
            websocket.send(json.dumps({"id": message["id"], "result": result}))

    def wait_for(self, predicate, seconds=17):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail("Background worker did not reach the expected state")

    def test_mcp_read_and_schema_without_model_or_desktop(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "usage", "arguments": {"action": "read_remaining_percent"}}},
        ]
        process = subprocess.run([sys.executable, str(ROOT / "tools/usage/server.py")],
                                 input="".join(json.dumps(r) + "\n" for r in requests),
                                 text=True, capture_output=True, env=self.env, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(responses[1]["result"]["tools"][0]["name"], "usage")
        self.assertEqual(responses[2]["result"]["structuredContent"]["windows"][0]["remaining_percent"], 50)
        self.assertEqual(self.consumed, 0)

    def test_real_worker_expires_after_idle_even_without_lifecycle_delivery(self):
        self.service.start(limit=1)
        process = self.store.read("hook")["process"]
        self.status = "idle"
        self.wait_for(lambda: self.store.read("hook") is None)
        self.wait_for(lambda: not process_alive(process), seconds=4)
        self.assertEqual(self.consumed, 0)

    def test_competing_tool_processes_create_only_one_worker(self):
        processes = []
        for owner in ("test-owner", "competitor"):
            process = subprocess.Popen([sys.executable, str(ROOT / "tools/usage/server.py"), "--call"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=self.env)
            process.stdin.write(json.dumps({"hook": "use_reset_automatically", "thread_id": owner}))
            process.stdin.close()
            process.stdin = None
            processes.append(process)
        results = []
        for process in processes:
            output, error = process.communicate(timeout=10)
            results.append(json.loads(output))
        self.assertEqual(sum("hook" in result for result in results), 1)
        self.assertEqual(sum("error" in result for result in results), 1)
        self.assertTrue(process_alive(self.store.read("hook")["process"]))
        self.assertEqual(self.consumed, 0)

    def test_real_worker_redeems_at_threshold_and_enforces_limit(self):
        self.used = 99
        self.service.start(limit=1)
        self.wait_for(lambda: self.store.read("hook") is None, seconds=6)
        self.assertEqual(self.consumed, 1)
        self.assertEqual(self.store.read("last_hook")["reason"], "usage_limit")

    def test_keep_running_worker_survives_stop_and_interrupt_cleans_it_up(self):
        self.service.start(keep_running=True)
        process = self.store.read("hook")["process"]
        self.status = "idle"
        record_event({"hook_event_name": "Stop", "session_id": "test-owner"}, self.store)
        time.sleep(3)
        self.assertTrue(process_alive(process))
        payload = {"hook_event_name": "Interrupt", "session_id": "test-owner"}
        run = subprocess.run([sys.executable, str(ROOT / "hooks/usage_lifecycle.py")],
                             input=json.dumps(payload), text=True, capture_output=True,
                             env=self.env, timeout=3)
        self.assertEqual(json.loads(run.stdout), {})
        self.wait_for(lambda: self.store.read("hook") is None, seconds=5)
        self.assertEqual(self.consumed, 0)


if __name__ == "__main__":
    unittest.main()
