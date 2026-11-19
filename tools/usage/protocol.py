"""Codex app-server transport and validated account usage responses."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from websockets.sync.client import connect, unix_connect

from .state import process_identity


class UsageError(RuntimeError):
    """A safe diagnostic that contains no provider response or credentials."""


class RpcError(UsageError):
    def __init__(self, code, retry_after=0):
        code = code if type(code) is int else "unknown"
        super().__init__(f"Codex request failed (code {code}); no reset success is assumed")
        self.code = code
        self.retry_after = retry_after


def remaining_usage(snapshot):
    """Keep each quota window distinct; missing usage must never mean exhausted."""
    buckets = snapshot.get("rateLimitsByLimitId")
    if buckets is None:
        single = snapshot.get("rateLimits")
        buckets = {single.get("limitId") or "codex": single} if single else {}
    if not isinstance(buckets, dict):
        raise ValueError("Invalid quota buckets")
    result = []
    for bucket_id, bucket in buckets.items():
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if window is None:
                continue
            used = window.get("usedPercent")
            if type(used) not in (int, float) or not math.isfinite(used) or used < 0:
                raise ValueError("Invalid usage percentage")
            result.append({"bucket": bucket_id, "window": name,
                           "remaining_percent": max(0, 100 - used),
                           "window_minutes": window.get("windowDurationMins"),
                           "resets_at": window.get("resetsAt")})
    return result


def reset_count(snapshot):
    """Use the authoritative count, never the potentially truncated detail rows."""
    summary = snapshot.get("rateLimitResetCredits")
    if summary is None:
        return None
    count = summary.get("availableCount")
    if type(count) is not int or count < 0:
        raise ValueError("Invalid available reset count")
    return count


def local_tcp_servers():
    """Discover only this user's Codex listeners, without reading process arguments."""
    servers = {}
    result = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=5, check=True)
    for line in result.stdout.splitlines():
        match = re.search(r'\("codex",pid=(\d+),', line)
        if not match:
            continue
        try:
            if Path("/proc", match[1]).stat().st_uid != os.getuid():
                continue
        except FileNotFoundError:
            continue
        address = line.split()[3]
        if address.startswith(("127.0.0.1:", "[::1]:")):
            servers["ws://" + address] = process_identity(int(match[1]))
    return servers


def local_endpoints():
    explicit = os.environ.get("CODEX_USAGE_ENDPOINT")
    if explicit:
        return [explicit]
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    endpoints = []
    socket = root / "app-server-control/app-server-control.sock"
    if socket.is_socket() and socket.stat().st_uid == os.getuid():
        endpoints.append("unix://" + str(socket))
    endpoints.extend(local_tcp_servers())
    return list(dict.fromkeys(endpoints))


class Client:
    """One bounded, serial RPC stream, with authentication owned by Codex."""

    def __init__(self, endpoint, timeout=15):
        self.endpoint, self.timeout, self.sequence = endpoint, timeout, 0
        self.socket = None

    def open(self):
        # Local transports avoid reading or copying Codex authentication secrets.
        options = dict(open_timeout=3, close_timeout=1, max_size=4 * 1024 * 1024)
        if self.endpoint.startswith("unix://"):
            path = self.endpoint.removeprefix("unix://")
            if Path(path).stat().st_uid != os.getuid():
                raise UsageError("Codex socket belongs to another user")
            self.socket = unix_connect(path, **options)
        else:
            url = urlsplit(self.endpoint)
            if (url.scheme != "ws" or url.hostname not in ("127.0.0.1", "::1", "localhost")
                    or not url.port or url.username or url.password or url.query or url.fragment
                    or url.path not in ("", "/")):
                raise UsageError("A local Codex app-server endpoint is required")
            self.socket = connect(self.endpoint, proxy=None, **options)
        try:
            self.call("initialize", {"clientInfo": {"name": "codex_usage", "version": "0.1.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.socket.send(json.dumps({"method": "initialized", "params": {}}))
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        if self.socket:
            self.socket.close()
            self.socket = None

    def call(self, method, params=None):
        # Reuse one deadline even if notifications arrive ahead of the response.
        if self.socket is None:
            raise UsageError("Codex connection is not open")
        self.sequence += 1
        request_id = self.sequence
        message = {"id": request_id, "method": method, "params": params or {}}
        deadline = time.monotonic() + self.timeout
        self.socket.send(json.dumps(message))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex request deadline reached")
            response = json.loads(self.socket.recv(timeout=remaining))
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = response["error"]
                data = error.get("data") or {}
                retry = data.get("retryAfterSeconds", 0) if isinstance(data, dict) else 0
                if not isinstance(retry, (int, float)) or not math.isfinite(retry):
                    retry = 0
                raise RpcError(error.get("code"), max(0, retry))
            result = response.get("result")
            if not isinstance(result, dict):
                raise UsageError("Invalid Codex response")
            return result

    def usage(self):
        return self.call("account/rateLimits/read", {"excludeResetCreditDetails": True})

    def thread_state(self, thread_id):
        response = self.call("thread/read", {"threadId": thread_id})
        status = response.get("thread", {}).get("status", {}).get("type")
        if status not in ("active", "idle", "notLoaded", "systemError"):
            raise UsageError("Thread status could not be verified")
        return status

    def has_thread(self, thread_id):
        cursor, seen = None, set()
        while True:
            page = self.call("thread/loaded/list", {"cursor": cursor, "limit": 100})
            if thread_id in page.get("data", []):
                return True
            cursor = page.get("nextCursor")
            if cursor is None:
                return False
            if cursor in seen:
                raise UsageError("Thread inventory repeated a pagination cursor")
            seen.add(cursor)


def stopped_on_local_servers(thread_id):
    # A dead original server is insufficient if another live server resumed the thread.
    if os.environ.get("CODEX_USAGE_ENDPOINT"):
        return False
    endpoints = local_endpoints()
    if not endpoints:
        return False
    for endpoint in endpoints:
        client = Client(endpoint)
        try:
            client.open()
            if client.has_thread(thread_id) and client.thread_state(thread_id) not in ("idle", "notLoaded"):
                return False
        finally:
            client.close()
    return True


def find_client(thread_id):
    """Require a unique live owner rather than routing by the selected UI thread."""
    matches = []
    for endpoint in local_endpoints():
        client = Client(endpoint)
        try:
            client.open()
            if client.has_thread(thread_id):
                matches.append(client)
            else:
                client.close()
        except Exception:
            client.close()
    if len(matches) == 1:
        return matches[0]
    for client in matches:
        client.close()
    raise UsageError("A unique running Codex server for this thread could not be verified")
