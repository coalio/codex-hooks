#!/usr/bin/env python3
"""Install the usage tool while preserving unrelated Codex configuration and hooks."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[2]


def merge_hooks(existing, source):
    merged = json.loads(json.dumps(existing))
    for event in ("Stop", "Interrupt", "SessionEnd"):
        handlers = [h for group in source["hooks"][event] for h in group["hooks"]
                    if "usage_lifecycle.py" in h.get("command", "")]
        groups = merged.setdefault("hooks", {}).setdefault(event, [])
        installed = [h for group in groups for h in group.get("hooks", [])]
        for handler in handlers:
            if handler not in installed:
                if any("usage_lifecycle.py" in h.get("command", "") for h in installed):
                    raise ValueError("Existing usage lifecycle configuration differs; review it before installing")
                groups.append({"hooks": [handler]})
    return merged


def configure_mcp(text, interpreter, server):
    config = tomllib.loads(text)
    expected = {"command": str(interpreter), "args": [str(server)], "tool_timeout_sec": 120}
    existing = config.get("mcp_servers", {}).get("codex_usage")
    if existing is not None:
        if existing != expected:
            raise ValueError("Existing codex_usage registration differs; review it before installing")
        return text
    return text.rstrip() + "\n\n[mcp_servers.codex_usage]\n" + "\n".join(
        key + " = " + json.dumps(value) for key, value in expected.items()) + "\n"


def install(destination, dependencies=True):
    destination = Path(destination).expanduser().resolve()
    package = destination / "tools/usage"
    interpreter = package / ".venv/bin/python"
    config_path, hooks_path = destination / "config.toml", destination / "hooks.json"
    source_hooks = json.loads((ROOT / "hooks.json").read_text())
    old_hooks = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
    new_hooks = json.dumps(merge_hooks(old_hooks, source_hooks), indent=2) + "\n"
    old_config = config_path.read_text() if config_path.exists() else ""
    new_config = configure_mcp(old_config, interpreter, package / "server.py")

    # Complete conflict checks before changing installed code or configuration.
    files = {"tools/__init__.py": (ROOT / "tools/__init__.py").read_bytes(),
             "hooks/usage_lifecycle.py": (ROOT / "hooks/usage_lifecycle.py").read_bytes(),
             "config.toml": new_config.encode(), "hooks.json": new_hooks.encode()}
    for path in (ROOT / "tools/usage").iterdir():
        if path.suffix == ".py" or path.name == "requirements.txt":
            files["tools/usage/" + path.name] = path.read_bytes()
    backup = destination / "backups" / ("usage-" + str(uuid.uuid4()))
    for name in files:
        path = destination / name
        if path.exists() and path.read_bytes() != files[name]:
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, saved)
            saved.chmod(0o600)

    # Install dependencies before publishing the MCP registration and lifecycle definitions.
    package.mkdir(parents=True, exist_ok=True)
    if dependencies:
        subprocess.run([sys.executable, "-m", "venv", str(package / ".venv")], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run([str(interpreter), "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(ROOT / "tools/usage/requirements.txt")], check=True, stdout=subprocess.DEVNULL)
    for name, content in files.items():
        if name in ("config.toml", "hooks.json"):
            continue
        atomic_write(destination / name, content)
    for name in ("config.toml", "hooks.json"):
        atomic_write(destination / name, files[name])
    return {"installed": True, "backup_created": backup.exists(), "hook_trust_check": "hooks/list",
            "mcp_server": "codex_usage"}


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".usage-install-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    args = parser.parse_args()
    print(json.dumps(install(args.codex_home)))
