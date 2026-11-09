import json
import tempfile
from pathlib import Path
import unittest

from tools.usage.install import configure_mcp, install, merge_hooks


class InstallTests(unittest.TestCase):
    def test_preserves_unrelated_configuration_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_config = '# keep this comment\nmodel = "chosen-model"\n[mcp_servers.existing]\ncommand = "existing"\n'
            original_hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-check"}]}]}}
            (root / "config.toml").write_text(original_config)
            (root / "hooks.json").write_text(json.dumps(original_hooks))
            install(root, dependencies=False)
            first = (root / "hooks.json").read_text()
            self.assertTrue((root / "config.toml").read_text().startswith(original_config))
            self.assertIn("existing-check", first)
            self.assertEqual(len(json.loads(first)["hooks"]["Stop"]), 2)
            install(root, dependencies=False)
            self.assertEqual((root / "hooks.json").read_text(), first)

    def test_different_existing_registration_is_not_overwritten(self):
        with self.assertRaises(ValueError):
            configure_mcp('[mcp_servers.codex_usage]\ncommand="other"\n', Path("python"), Path("server"))
