"""Cross-process ownership, atomic state, and PID-reuse-safe process checks."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import time


def thread_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("A valid originating thread_id is required")
    return value


def process_identity(pid=None):
    pid = os.getpid() if pid is None else pid
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "start": fields[19],
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, IndexError):
        return None


def process_alive(identity):
    return bool(identity and process_identity(identity["pid"]) == identity)


def stop_process(identity):
    # Pin the PID before verifying its birth identity, so PID reuse cannot kill a peer.
    if not identity or identity["pid"] == os.getpid():
        return
    try:
        descriptor = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return
    try:
        if process_alive(identity):
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("CODEX_USAGE_STATE_DIR") or
                         Path.home() / ".local/state/codex-usage")
        if self.root.is_symlink():
            raise ValueError("Usage state directory must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_uid != os.getuid():
            raise ValueError("Usage state directory belongs to another user")
        self.root.chmod(0o700)

    def path(self, name):
        return self.root / (thread_key(name) + ".json")

    def read(self, name):
        try:
            return json.loads(self.path(name).read_text())
        except FileNotFoundError:
            return None

    def write(self, name, value):
        # A durable rename prevents a crash from exposing half-written ownership or intent.
        descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(value, output, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path(name))
            self._sync()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def delete(self, name):
        self.path(name).unlink(missing_ok=True)
        self._sync()

    def _sync(self):
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def guard(self, timeout=65):
        # The arbitration inode stays in place; the separate hook ownership file is removed.
        descriptor = os.open(self.root / ".guard", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Another usage operation is still running")
                    time.sleep(0.05)
            yield
        finally:
            os.close(descriptor)
