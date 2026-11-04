#!/usr/bin/env python3
"""Offline setup checks; no adapter, server, model, trust, or login is invoked."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import ieodo_setup as setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ieodo setup ")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project space"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.config = self.base / "private space" / "config.json"
        self.codex = self.root / ".codex/hooks.json"
        self.claude = self.root / ".claude/settings.local.json"
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.env_patch = patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.home_patch.stop()
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def invoke(self, *flags, harness="both", project="demo", root=True, config=None):
        argv = ["--project", project, "--harness", harness, "--config", str(config or self.config)]
        if root:
            argv += ["--project-dir", str(self.root)]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = setup.main(argv + list(flags))
        return code, json.loads(output.getvalue())

    def snapshot(self):
        return {str(path.relative_to(self.base)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.base.rglob("*") if path.is_file()}

    def test_first_install_both_check_and_noop(self):
        code, result = self.invoke("--apply")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["configured"])
        self.assertEqual(result["runtime_validation"], "not_checked")
        self.assertEqual(json.loads(self.config.read_text())["projects"], [{"root": str(self.root), "project": "demo"}])
        for path in (self.config, self.codex, self.claude):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = self.snapshot()
        self.assertEqual(self.invoke("--check")[0], 0)
        code, result = self.invoke("--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["changed_paths"], [])
        self.assertEqual(result["backups"], [])
        self.assertEqual(before, self.snapshot())
        self.assertFalse(list(self.base.rglob("*.sqlite3")))

    def test_dryrun_and_unconfigured_check_do_not_write(self):
        before = self.snapshot()
        code, result = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(set(result["changed_paths"]), {str(self.config), str(self.codex), str(self.claude)})
        self.assertFalse(result["configured"])
        self.assertEqual(result["harnesses"]["codex"]["state"], "missing")
        self.assertEqual(self.invoke("--check")[0], 1)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.config.parent.exists())

    def test_preserve_unrelated_projects_settings_handlers_and_work(self):
        other = self.base / "other"
        other.mkdir()
        original = {"projects": [{"root": str(other), "project": "other", "custom": [1]},
                                 {"root": str(self.root), "project": "demo", "work_id": "handoff:abc", "custom": True}],
                    "server_url": "http://localhost:7341", "flush_limit": 2, "unknown": {"private": "do-not-print"}}
        self.write(self.config, original)
        native = {"permissions": {"allow": ["Read"]}, "env": {"SOME_KEY": "private-dont-print"},
                  "hooks": {"Stop": [{"matcher": "custom", "hooks": [
                      {"type": "command", "command": "echo unrelated"},
                      {"type": "command", "command": "python /old/ieodo_hooks.py --owner " + setup.OWNER}]}],
                            "OtherEvent": [{"matcher": "keep-empty", "hooks": []}]}}
        self.write(self.claude, native)
        code, result = self.invoke("--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(json.loads(self.config.read_text()), original)
        saved = json.loads(self.claude.read_text())
        self.assertEqual(saved["permissions"], native["permissions"])
        self.assertEqual(saved["env"], native["env"])
        self.assertEqual(saved["hooks"]["OtherEvent"], native["hooks"]["OtherEvent"])
        self.assertEqual(saved["hooks"]["Stop"][0]["hooks"], [{"type": "command", "command": "echo unrelated"}])
        backups = list(self.claude.parent.glob("*.ieodo-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), native)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn("do-not-print", json.dumps(result))
        self.assertNotIn("private-dont-print", json.dumps(result))
        self.assertEqual(self.invoke("--check")[0], 0)

    def test_malformed_native_prevents_partial_config_or_other_harness_writes(self):
        self.claude.parent.mkdir()
        self.claude.write_text('{"hooks":{"Stop":[{"hooks":[7]}]}}')
        before = self.snapshot()
        code, result = self.invoke("--apply")
        self.assertEqual(code, 1, result)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.config.parent.exists())
        self.assertFalse(self.codex.parent.exists())

    def test_native_write_failure_preserves_the_existing_registration(self):
        original_config = {"projects": [{"root": str(self.root), "project": "demo", "work_id": "old"}]}
        original_native = {"permissions": {"allow": ["Read"]}}
        self.write(self.config, original_config)
        self.write(self.codex, original_native)
        real_write = setup.atomic_write

        def fail_codex(path, content):
            if path == self.codex:
                raise OSError("injected Codex write failure")
            real_write(path, content)

        with patch.object(setup, "atomic_write", side_effect=fail_codex):
            code, result = self.invoke("--apply")
        self.assertEqual(code, 1, result)
        self.assertFalse(result["partial_application"])
        self.assertEqual(result["applied_paths"], [])
        self.assertEqual(result["failed_path"], str(self.codex))
        self.assertEqual(json.loads(self.config.read_text()), original_config)
        self.assertEqual(json.loads(self.codex.read_text()), original_native)
        self.assertFalse(self.claude.exists())
        self.assertEqual(len(result["backups"]), 1)
        self.assertEqual(json.loads(Path(result["backups"][0]).read_text()), original_native)
        self.assertIn("Rerun the same", result["repair_hint"])
        self.assertFalse(list(self.config.parent.glob("config.json.ieodo-backup-*")))

    def test_late_write_failure_reports_applied_paths_and_recoverable_backups(self):
        # A new registration makes the shared config the last write.
        originals = {self.config: {"projects": []},
                     self.codex: {"custom": "codex"}, self.claude: {"custom": "claude"}}
        for path, value in originals.items():
            self.write(path, value)
        real_write = setup.atomic_write

        def fail_config(path, content):
            if path == self.config:
                raise OSError("injected shared config write failure")
            real_write(path, content)

        with patch.object(setup, "atomic_write", side_effect=fail_config):
            code, result = self.invoke("--apply")
        self.assertEqual(code, 1, result)
        self.assertTrue(result["partial_application"])
        self.assertFalse(result["configured"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["applied_paths"], [str(self.codex), str(self.claude)])
        self.assertEqual(result["failed_path"], str(self.config))
        self.assertEqual(result["changed_paths"][-1], str(self.config))
        self.assertEqual(json.loads(self.config.read_text()), originals[self.config])
        self.assertEqual(len(result["backups"]), 3)
        for path, backup in zip((self.codex, self.claude, self.config), result["backups"]):
            self.assertEqual(json.loads(Path(backup).read_text()), originals[path])
        for harness, path in (("codex", self.codex), ("claude", self.claude)):
            self.assertIn("hooks", json.loads(path.read_text()))
            self.assertTrue(result["harnesses"][harness]["hooks_present"])
        self.assertIn("not a multi-file transaction", result["repair_hint"])
        self.assertEqual(self.invoke("--apply")[0], 0)
        self.assertEqual(json.loads(self.config.read_text())["projects"][0]["project"], "demo")
        before = self.snapshot()
        self.assertEqual(self.invoke("--apply")[0], 0)
        self.assertEqual(before, self.snapshot())

    def test_malformed_configs_rejected_before_any_write(self):
        for value in ([], {"projects": {}}, {"projects": [None]}, {"projects": [{"root": "relative", "project": "x"}]},
                      {"flush_limit": "bad"}, {"state_dir": None}, {"server_url": "https://127.0.0.1"}):
            with self.subTest(value=value):
                self.write(self.config, value)
                before = self.snapshot()
                self.assertEqual(self.invoke("--apply")[0], 1)
                self.assertEqual(before, self.snapshot())
        self.config.write_text('{"projects":[],"projects":[]}')
        self.assertEqual(self.invoke("--apply")[0], 1)
        self.config.write_text('{broken')
        self.assertEqual(self.invoke("--apply")[0], 1)
        self.assertFalse(self.codex.exists())

    def test_duplicate_or_conflicting_exact_roots_rejected(self):
        for entries in ([{"root": str(self.root), "project": "other"}],
                        [{"root": str(self.root), "project": "demo"}] * 2,
                        [{"root": str(self.root), "project": "demo"}, {"root": str(self.root / "."), "project": "other"}]):
            with self.subTest(entries=entries):
                self.write(self.config, {"projects": entries})
                before = self.snapshot()
                self.assertEqual(self.invoke("--apply")[0], 1)
                self.assertEqual(before, self.snapshot())

    def test_different_project_ancestor_mapping_cannot_be_overridden(self):
        self.write(self.config, {"projects": [{"root": str(self.base), "project": "other"}]})
        before = self.snapshot()
        self.assertEqual(self.invoke("--apply")[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_explicit_same_slug_additional_root_allowed(self):
        other = self.base / "worktree"
        self.write(self.config, {"projects": [{"root": str(other), "project": "demo"}]})
        self.assertEqual(self.invoke("--apply")[0], 0)
        self.assertEqual(len(json.loads(self.config.read_text())["projects"]), 2)

    def test_paths_spaces_and_runtime_location_quoted_exactly(self):
        runtime = self.base / "installed runtime with spaces"
        runtime.mkdir()
        for name in ("ieodo_setup.py", "ieodo_hooks.py"):
            shutil.copyfile(Path(setup.__file__).with_name(name), runtime / name)
        result = subprocess.run([sys.executable, str(runtime / "ieodo_setup.py"), "--project-dir", str(self.root),
                                 "--project", "demo", "--harness", "codex", "--config", str(self.config), "--apply"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        native = json.loads(self.codex.read_text())
        expected = [str(Path(sys.executable).resolve()), str(runtime / "ieodo_hooks.py"), "--harness", "codex",
                    "--config", str(self.config), "--owner", setup.OWNER, "hook"]
        for event in setup.EVENTS:
            self.assertEqual(shlex.split(native["hooks"][event][0]["hooks"][0]["command"]), expected)
        self.assertEqual(json.loads(result.stdout)["harnesses"]["codex"]["health_command"], expected[:-1] + ["status"])

    def test_disabled_native_hooks_are_reported_and_never_enabled(self):
        self.write(self.claude, {"disableAllHooks": True})
        self.write(self.root / ".codex/config.toml", {})
        (self.root / ".codex/config.toml").write_text('[features]\nhooks = false\n')
        code, result = self.invoke("--apply")
        self.assertEqual(code, 1)
        self.assertFalse(result["configured"])
        self.assertTrue(result["applied"])
        for state in result["harnesses"].values():
            self.assertEqual(state["state"], "disabled")
            self.assertTrue(state["hooks_present"])
        before = self.snapshot()
        self.assertEqual(self.invoke("--check")[0], 1)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(json.loads(self.claude.read_text())["disableAllHooks"])
        self.assertEqual((self.root / ".codex/config.toml").read_text(), '[features]\nhooks = false\n')

    def test_antigravity_hooks_go_to_the_project_and_keep_other_hooks(self):
        agents = self.root / ".agents/hooks.json"
        self.write(agents, {"lint": {"PostToolUse": [{"matcher": "*", "hooks": [{"command": "lint"}]}]}})
        code, result = self.invoke("--apply", harness="antigravity")
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["harnesses"]), {"antigravity"})
        saved = json.loads(agents.read_text())
        self.assertEqual(saved["lint"], {"PostToolUse": [{"matcher": "*", "hooks": [{"command": "lint"}]}]})
        stop = saved["ieodo-continuity"]["Stop"][0]["command"]
        self.assertIn("--harness antigravity", stop)
        self.assertTrue(stop.endswith(" hook --event Stop"), stop)
        self.assertFalse(self.codex.exists() or self.claude.exists())
        self.assertEqual(self.invoke("--check", harness="antigravity")[0], 0)
        # A user's off switch stays and is reported.
        for groups in saved["ieodo-continuity"].values():
            groups[0]["enabled"] = False
        self.write(agents, saved)
        code, result = self.invoke("--apply", harness="antigravity")
        self.assertEqual(result["harnesses"]["antigravity"]["state"], "disabled")
        self.assertTrue(all(g[0]["enabled"] is False for g in json.loads(agents.read_text())["ieodo-continuity"].values()))

    def test_all_installs_every_harness(self):
        code, result = self.invoke("--apply", harness="all")
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["harnesses"]), {"codex", "claude", "antigravity"})
        self.assertTrue((self.root / ".agents/hooks.json").exists())

    def test_codex_deprecated_flag_managed_policy_and_primary_override(self):
        path = self.root / ".codex/config.toml"
        path.parent.mkdir()
        for text, disabled in (('[features]\ncodex_hooks=false\n', True),
                               ('allow_managed_hooks_only=true\n[features]\nhooks=true\n', True),
                               ('[features]\nhooks=true\ncodex_hooks=false\n', False)):
            path.write_text(text)
            code, result = self.invoke("--apply", harness="codex")
            self.assertEqual(code, int(disabled), result)
            self.assertEqual(result["harnesses"]["codex"]["state"], "disabled" if disabled else "configured")

    def test_malformed_native_toml_and_claude_settings_prevent_write(self):
        path = self.root / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text('[features]\nhooks = ')
        before = self.snapshot()
        self.assertEqual(self.invoke("--apply")[0], 1)
        self.assertEqual(before, self.snapshot())
        path.unlink()
        self.write(self.root / ".claude/settings.json", {"hooks": [7]})
        before = self.snapshot()
        self.assertEqual(self.invoke("--apply")[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_no_global_trust_login_or_mcp_mutation(self):
        for relative in (".codex/config.toml", ".codex/hooks.json", ".codex/auth.json", ".claude/settings.json", ".claude.json", ".mcp.json"):
            self.write(self.home / relative, {"trust": "sentinel", "secret": "sentinel"})
        before = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(self.invoke("--apply")[0], 0)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
        for path in (self.home / ".codex/hooks.json", self.home / ".claude/settings.json", self.root / ".mcp.json"):
            self.assertEqual(self.invoke("--apply", config=path)[0], 1)

    def test_symlinked_native_directory_cannot_edit_another_project(self):
        other = self.base / "unrelated"
        other.mkdir()
        (self.root / ".claude").symlink_to(other, target_is_directory=True)
        before = self.snapshot()
        self.assertEqual(self.invoke("--apply")[0], 1)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.codex.exists())

    def test_unsafe_broad_relative_or_missing_project_roots_rejected(self):
        for root in (str(self.home), "/", str(self.base / "missing"), "relative"):
            code, result = self.invoke("--apply", "--project-dir", root)
            self.assertEqual(code, 1, result)
        self.assertFalse(self.config.exists())

    def test_server_url_loopback_origin_validation(self):
        for url in ("http://127.0.0.1:7341", "http://localhost", "http://[::1]:7341/"):
            self.write(self.config, {"server_url": url})
            self.assertEqual(self.invoke()[0], 0)
        for url in ("https://localhost", "http://example.org", "http://localhost/path", "http://user@localhost", "http://localhost?x=1", "http://localhost:bad"):
            self.write(self.config, {"server_url": url})
            before = self.snapshot()
            self.assertEqual(self.invoke("--apply")[0], 1)
            self.assertEqual(before, self.snapshot())

    def test_invalid_project_names_rejected(self):
        for project in ("UPPER", "a/b", "", "a" * 65):
            self.assertEqual(self.invoke("--apply", project=project)[0], 1)
        self.assertFalse(self.config.exists())

    def test_default_config_respects_ieodo_home(self):
        default_home = self.base / "custom ieodo"
        with patch.dict(os.environ, {"IEODO_HOME": str(default_home)}), contextlib.redirect_stdout(io.StringIO()) as output:
            code = setup.main(["--project-dir", str(self.root), "--project", "demo", "--harness", "codex", "--apply"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["config"], str(default_home / "continuity/config.json"))

    def test_empty_or_whitespace_ieodo_home_uses_default(self):
        for value in ("", "  ", "\t"):
            with self.subTest(value=value), patch.dict(os.environ, {"IEODO_HOME": value}):
                self.assertEqual(setup.default_config(), str(self.home / ".ieodo/continuity/config.json"))
        with patch.dict(os.environ, {}, clear=True), patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(setup.default_config(), str(self.home / ".ieodo/continuity/config.json"))

    def test_concurrent_shared_config_preserves_both_project_registrations(self):
        second = self.base / "second project"
        second.mkdir()
        processes = []
        try:
            with setup.configuration_lock(self.config):
                for root, project in ((self.root, "demo"), (second, "second")):
                    processes.append(subprocess.Popen([
                        sys.executable, str(Path(setup.__file__).resolve()), "--project-dir", str(root),
                        "--project", project, "--harness", "both", "--config", str(self.config), "--apply"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                time.sleep(0.2)
                self.assertTrue(all(process.poll() is None for process in processes))
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertTrue(json.loads(stdout)["configured"])
            projects = json.loads(self.config.read_text())["projects"]
            self.assertEqual({(entry["root"], entry["project"]) for entry in projects},
                             {(str(self.root), "demo"), (str(second), "second")})
            before = self.snapshot()
            self.assertEqual(self.invoke("--apply")[0], 0)
            self.assertEqual(before, self.snapshot())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_configuration_lock_times_out_and_does_not_unlink_shared_inode(self):
        with setup.configuration_lock(self.config):
            before = time.monotonic()
            with self.assertRaisesRegex(ValueError, "setup is busy"):
                with setup.configuration_lock(self.config, timeout=0.1):
                    self.fail("a second lock must wait")
            self.assertLess(time.monotonic() - before, 1)
        lock = self.config.with_name(self.config.name + ".ieodo-setup.lock")
        inode = lock.stat().st_ino
        with setup.configuration_lock(self.config):
            self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_git_root_and_worktree_inference_is_conservative(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        nested = self.root / "nested"
        nested.mkdir()
        with patch.object(Path, "cwd", return_value=nested):
            self.assertEqual(self.invoke("--apply", root=False)[0], 0)
        self.assertEqual(json.loads(self.config.read_text())["projects"][0]["root"], str(self.root))
        with patch.object(Path, "cwd", return_value=self.base):
            self.assertEqual(self.invoke(root=False)[0], 1)
        # Existing same-slug registration must not silently expand to a second inferred repo.
        second = self.base / "second"
        subprocess.run(["git", "init", "-q", str(second)], check=True)
        with patch.object(Path, "cwd", return_value=second):
            code, result = self.invoke("--apply", root=False)
            self.assertEqual(code, 1, result)
            self.assertIn("explicit --project-dir", result["error"])
        self.assertFalse((second / ".codex").exists())


if __name__ == "__main__":
    unittest.main()
