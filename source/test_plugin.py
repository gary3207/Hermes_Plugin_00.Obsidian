"""Plugin registration checks; no live home, Vault, scheduler or model calls."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent


class Context:
    def __init__(self):
        self.prompts = []
        self.skills = []
        self.commands = []

    def register_system_prompt_section(self, *args, **kwargs):
        self.prompts.append((args, kwargs))

    def register_skill(self, *args):
        self.skills.append(args)

    def register_cli_command(self, **kwargs):
        self.commands.append(kwargs)


class PluginCheck(unittest.TestCase):
    def load(self, root):
        path = SOURCE / "__init__.py"
        self.assertTrue(path.is_file(), "Portable plugin entrypoint is missing")
        shutil.copyfile(path, root / "__init__.py")
        (root / "SKILL.md").write_text("---\nname: hermes-archive-records\n---\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("archive_plugin_check", root / "__init__.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_configured_session_has_actual_path_and_bundled_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = self.load(root)
            settings = {"archive_root": str(root / "vault/AI/Work"), "vault_path": str(root / "vault"), "archive_folder": "AI/Work"}
            (root / "archive-settings.json").write_text(json.dumps(settings), encoding="utf-8")
            ctx = Context()
            module.register(ctx)
            self.assertEqual(len(ctx.prompts), 1)
            text = ctx.prompts[0][0][1]
            self.assertIn(settings["archive_root"], text)
            self.assertIn("hermes-archive-records", text)
            self.assertIn("generated: true", text)
            self.assertIn("현재 요약", text)
            self.assertIn("의미 있는 전환", text)
            self.assertIn("새 승인 게이트", text)
            self.assertEqual((SOURCE / "__init__.py").read_bytes(), (SOURCE / "archive-profile/__init__.py").read_bytes())
            self.assertLessEqual(len(text), ctx.prompts[0][1]["max_chars"])
            self.assertEqual(ctx.skills[0][0], "hermes-archive-records")
            self.assertEqual(ctx.commands[0]["name"], "obsidian-archive")

    def test_unconfigured_plugin_does_not_guess_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = self.load(Path(tmp))
            ctx = Context()
            module.register(ctx)
            self.assertEqual(ctx.prompts, [])
            self.assertEqual(len(ctx.skills), 1)
            self.assertEqual(len(ctx.commands), 1)

    def test_cli_exposes_only_two_required_setup_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = self.load(Path(tmp))
            ctx = Context()
            module.register(ctx)
            parser = argparse.ArgumentParser()
            ctx.commands[0]["setup_fn"](parser)
            args = parser.parse_args(["setup", "--vault", tmp, "--folder", "AI/Work"])
            self.assertEqual(args.vault, tmp)
            self.assertEqual(args.folder, "AI/Work")
            self.assertEqual(args.archive_action, "setup")
            for command in ["status", "uninstall"]:
                self.assertEqual(parser.parse_args([command]).archive_action, command)

    def test_malformed_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = self.load(root)
            (root / "archive-settings.json").write_text('{"archive_root": "X\\nFake instructions", "archive_folder": "AI/Work"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                module.register(Context())


if __name__ == "__main__":
    unittest.main(verbosity=2)
