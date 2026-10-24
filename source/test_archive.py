"""Runnable standard-library check: python source/test_archive.py."""
from contextlib import closing
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
import os
import shutil


def discover_native():
    homes = [Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))]
    if os.environ.get("LOCALAPPDATA"):
        homes.append(Path(os.environ["LOCALAPPDATA"]) / "hermes")
    candidates = [p for home in homes for p in home.glob("installs/*/environments/*/workspace")
                  if (p / "hermes_cli/session_export_md.py").is_file()]
    if not candidates:
        raise RuntimeError("Installed Hermes exporter required for real archive tests")
    return max(candidates, key=lambda p: p.stat().st_mtime)


NATIVE = discover_native()


def seed(home, session_id="s1", profile="default"):
    home.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(home / "state.db")) as db, db:
        db.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, source TEXT, cwd TEXT,
            profile_name TEXT, started_at REAL, ended_at REAL, parent_session_id TEXT,
            last_activity_at REAL, end_reason TEXT, message_count INTEGER, tool_call_count INTEGER);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL, active INTEGER, compacted INTEGER,
            display_kind TEXT, reasoning TEXT);
        """)
        now = time.time()
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (session_id, "합성 검사", "desktop", None, profile, now, None, None, now, None, 4, 1))
        rows = [
            (1, session_id, "user", "테스트 요청 password=OnlySyntheticSecretForArchiveTest", None, None, now, 1, 0, None, None),
            (2, session_id, "assistant", "확인합니다.", '[{"function":{"name":"terminal","arguments":"{\\"command\\":\\"false\\"}"}}]', None, now, 1, 0, None, "INTERNAL_REASONING_MUST_NOT_EXPORT"),
            (3, session_id, "tool", '{"exit_code":1,"error":"synthetic failure"}', None, "terminal", now, 1, 0, None, None),
            (4, session_id, "assistant", "작업이 차단되었습니다.", None, None, now, 1, 0, None, None),
        ]
        db.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)


def load_archive():
    path = SOURCE / "archive.py"
    assert path.exists(), "Missing archive.py: session archive feature is not implemented"
    spec = importlib.util.spec_from_file_location("archive", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArchiveCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-archive-", dir=SOURCE)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.vault = self.root / "vault" / "Hermes"
        self.vault.parent.mkdir(parents=True)
        self.cfg = {"base_home": str(self.home), "archive_root": str(self.vault),
                    "runtime": str(self.root / "runtime"), "native_workspace": str(NATIVE),
                    "source": str(SOURCE), "bootstrap_profiles": False}
        seed(self.home)

    def tearDown(self):
        for alias in reversed(getattr(self, "directory_links", [])):
            os.rmdir(alias) if os.name == "nt" else alias.unlink()
        self.tmp.cleanup()

    def test_readable_paths_nest_subagents_and_retire_moved_files(self):
        archive = load_archive()
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            now = time.time()
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("c1", "Subagent: Read #3", "desktop", None, "default", now + 1, now, "s1", now, None, 1, 0))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", (9, "c1", "user", "하위 작업", None, None, now, 1, 0, None, None))
        self.assertEqual(archive.run(self.cfg)["errors"], [])
        day = time.strftime("%Y-%m-%d")
        top = next((self.vault / "40_Sessions" / day[:7]).glob(f"{day} * 합성 검사"))
        self.assertTrue((top / f"{top.name}.md").exists())
        self.assertTrue((top / "대화.md").exists() and (top / "도구 실행.md").exists())
        child = list((top / "Subagents").glob("* Read 3.md"))
        self.assertEqual(len(child), 1)
        home = (self.vault / "00_Home.md").read_text(encoding="utf-8")
        self.assertIn("↳ Subagent 1", home)
        self.assertNotIn("Read 3", home)
        self.assertIn("합성 검사", (self.vault / "40_Sessions" / day[:7] / f"{day}.md").read_text(encoding="utf-8"))
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.execute("UPDATE sessions SET title='새 제목: a/b' WHERE id='s1'")
        self.assertEqual(archive.run(self.cfg)["errors"], [])
        self.assertFalse(top.exists(), "Moved session left its old generated folder")
        moved = next((self.vault / "40_Sessions" / day[:7]).glob(f"{day} * 새 제목 a b"))
        self.assertEqual(len(list((moved / "Subagents").glob("* Read 3.md"))), 1)

    def test_summary_records_model_effort_and_tokens(self):
        archive = load_archive()
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            for col in ("model TEXT", "model_config TEXT", "input_tokens INTEGER", "output_tokens INTEGER", "estimated_cost_usd REAL"):
                db.execute(f"ALTER TABLE sessions ADD COLUMN {col}")
            db.execute("UPDATE sessions SET model=?, model_config=?, input_tokens=1234, output_tokens=56, estimated_cost_usd=0.5",
                       ("gpt-x", '{"reasoning_config":{"effort":"high"},"_usage_anchor":{"base_last_fp":"FP_MUST_NOT_EXPORT"}}'))
        self.assertEqual(archive.run(self.cfg)["errors"], [])
        summary = next(self.vault.glob("40_Sessions/*/*/*합성 검사.md")).read_text(encoding="utf-8")
        self.assertIn("모델: `gpt-x` · Effort: `high`", summary)
        self.assertIn("입력 1,234 · 출력 56", summary)
        self.assertIn('effort: "high"', summary)
        self.assertNotIn("FP_MUST_NOT_EXPORT", summary)

    def test_nested_folder_links_preserve_original_conversation(self):
        archive = load_archive()
        self.cfg.pop("archive_root")
        self.cfg.update(vault_path=str(self.vault.parent), archive_folder="AI/Hermes")
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.execute("UPDATE messages SET content=? WHERE id=1", ("Original [[Hermes/keep]] FROM \"Hermes/keep\"",))
        result = archive.run(self.cfg)
        self.assertEqual(result["errors"], [])
        root = self.vault.parent / "AI/Hermes"
        home = (root / "00_Home.md").read_text(encoding="utf-8")
        self.assertIn('FROM "AI/Hermes/40_Sessions"', home)
        for path in root.rglob("*.md"):
            if path.name != "대화.md" and not path.name.endswith("합성 검사.md"):
                self.assertNotIn("[[Hermes/", path.read_text(encoding="utf-8"))
        conversation = next(root.rglob("대화.md")).read_text(encoding="utf-8")
        self.assertIn('Original [[Hermes/keep]] FROM "Hermes/keep"', conversation)
        self.assertIn("[[AI/Hermes/40_Sessions/", home)

    def test_invalid_folder_rejected_before_writes(self):
        archive = load_archive()
        for folder in ["", ".", "..", "../outside", "/absolute", "AI/../Hermes", "AI|x", "AI[x]", 'AI"x', "AI\nx", "AI:x"]:
            with self.subTest(folder=folder):
                cfg = {**self.cfg, "vault_path": str(self.vault.parent), "archive_folder": folder}
                cfg.pop("archive_root")
                with self.assertRaises((ValueError, FileNotFoundError)):
                    archive.Archive(cfg)
                self.assertFalse(Path(cfg["runtime"]).exists())
        cfg = {**self.cfg, "vault_path": str(self.vault.parent), "archive_folder": "Different"}
        with self.assertRaises(ValueError):
            archive.Archive(cfg)
        self.assertFalse(Path(cfg["runtime"]).exists())

    def test_reinstall_recovers_private_mirror_and_manual_notes(self):
        archive = load_archive()
        self.test_profiles_groups_and_project_links()
        custom = self.vault / "10_Projects/test-handwritten.md"
        custom.write_text("handwritten", encoding="utf-8")
        mirror_path = self.vault / "60_Operations/.archive-state.json"
        self.assertTrue(mirror_path.exists(), "Portable ownership mirror missing")
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
        self.assertEqual(set(mirror), {"owner", "version", "files", "sessions", "projects", "groups"})
        self.assertNotIn("collector_digest", json.dumps(mirror))
        self.assertNotIn("signature", json.dumps(mirror))
        for db in self.home.rglob("*.db"):
            db.unlink()
        (self.vault / "60_Operations/20_Project-Map.md").unlink()
        self.cfg["runtime"] = str(self.root / "reinstalled-runtime")
        result = archive.run(self.cfg)
        self.assertEqual((result["sessions"], result["projects"], result["groups"]), (2, 1, 1))
        self.assertEqual(result["errors"], [])
        self.assertIn("합성 검사", (self.vault / "00_Home.md").read_text(encoding="utf-8"))
        self.assertIn("테스트 프로젝트", (self.vault / "60_Operations/20_Project-Map.md").read_text(encoding="utf-8"))
        self.assertEqual(custom.read_text(encoding="utf-8"), "handwritten")
        generated = next(self.vault.rglob("*합성 검사.md"))
        generated.write_text("manual edit", encoding="utf-8")
        restored = archive.Archive(self.cfg)
        with self.assertRaisesRegex(RuntimeError, "Manual edit conflict"):
            restored.write(generated.relative_to(self.vault), "replacement")

    def test_mirror_corruption_and_escaping_ownership_fail_closed(self):
        archive = load_archive()
        archive.run(self.cfg)
        mirror_path = self.vault / "60_Operations/.archive-state.json"
        self.assertTrue(mirror_path.exists(), "Portable mirror missing")
        original = json.loads(mirror_path.read_text(encoding="utf-8"))
        self.cfg["runtime"] = str(self.root / "clean-runtime")
        for value in ["{", "null", "[]", json.dumps({**original, "owner": "foreign"}),
                      json.dumps({**original, "files": {"../outside.md": "a" * 64}}),
                      json.dumps({**original, "sessions": {"x": {"path": "../outside.md"}}})]:
            mirror_path.write_text(value, encoding="utf-8")
            with self.assertRaises((ValueError, RuntimeError)):
                archive.Archive(self.cfg)
            self.assertEqual(mirror_path.read_text(encoding="utf-8"), value)

    def test_mirror_edit_and_runtime_root_collision_are_preserved(self):
        archive = load_archive()
        archive.run(self.cfg)
        mirror = self.vault / "60_Operations/.archive-state.json"
        self.assertTrue(mirror.exists(), "Portable mirror missing")
        holder = archive.Archive(self.cfg)
        mirror.write_text(mirror.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "[Mm]irror.*conflict"):
            holder.save_state()
        with self.assertRaises(RuntimeError):
            archive.Archive(self.cfg)
        different = {**self.cfg, "archive_root": str(self.vault.parent / "Other")}
        with self.assertRaisesRegex(RuntimeError, "root"):
            archive.Archive(different)
        self.assertFalse((self.vault.parent / "Other").exists())

    def test_native_helpers_discover_current_install_without_fixed_id(self):
        archive = load_archive()
        import subprocess
        code = "import archive; archive.native_helpers(" + repr({"base_home": str(NATIVE.parents[4]), "native_workspace": str(self.root / "removed-install")}) + ")"
        process = subprocess.run([sys.executable, "-c", code], cwd=SOURCE,
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        # Already importable helpers are preferred even if no workspace is configured.
        sys.path.insert(0, str(NATIVE))
        try:
            redact, render = archive.native_helpers({"base_home": str(self.home)})
            self.assertTrue(callable(redact))
            self.assertTrue(callable(render))
        finally:
            sys.path.remove(str(NATIVE))

    def test_invalid_cli_config_does_not_create_runtime(self):
        import subprocess
        cfg = {**self.cfg, "vault_path": str(self.vault.parent), "archive_folder": "../outside"}
        config = self.root / "bad-config.json"
        config.write_text(json.dumps(cfg), encoding="utf-8")
        process = subprocess.run([sys.executable, str(SOURCE / "archive.py"), "--config", str(config)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertFalse(Path(cfg["runtime"]).exists(), "Invalid path must be rejected before lock/fatal writes")

    def test_mirror_metadata_is_sanitized_and_runtime_metadata_excluded(self):
        archive = load_archive()
        holder = archive.Archive(self.cfg)
        holder.session("default", {"id": "private", "title": "normal"}, [])
        holder.state["sessions"]["default:private"]["cwd"] = "password=syntheticMirrorSecret"
        holder.state["sessions"]["default:private"]["unexpected"] = "must not leave runtime"
        holder.state.update(bootstrap={"profile": {"secret": "not mirrored"}}, sources={"fingerprint": "not mirrored"})
        holder.save_state()
        mirror = holder.mirror_path.read_text(encoding="utf-8")
        self.assertNotIn("syntheticMirrorSecret", mirror)
        self.assertNotIn("must not leave runtime", mirror)
        self.assertNotIn("not mirrored", mirror)

    def test_archive_symlink_escape_is_rejected(self):
        import subprocess
        archive = load_archive()
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.vault.parent / "AI"
        try:
            alias.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            process = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(outside)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
        cfg = {**self.cfg, "vault_path": str(self.vault.parent), "archive_folder": "AI/Hermes"}
        cfg.pop("archive_root")
        with self.assertRaises(ValueError):
            archive.Archive(cfg)
        self.assertFalse(Path(cfg["runtime"]).exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_native_export_never_falls_back_to_unredacted(self):
        from unittest.mock import patch
        archive = load_archive()
        with patch.object(archive.importlib, "import_module", side_effect=ImportError), patch.object(archive.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "refusing an unredacted"):
                archive.native_helpers({"base_home": str(self.home)})

    def test_explicitly_disabled_profile_stays_disabled(self):
        archive = load_archive()
        worker = self.home / "profiles/disabled"
        worker.mkdir(parents=True)
        (worker / "config.yaml").write_text("plugins:\n  enabled: []\n  disabled: [hermes-archive-context]\n", encoding="utf-8")
        holder = archive.Archive(self.cfg)
        statuses = archive.bootstrap_profiles(holder, [("disabled", worker)])
        self.assertEqual(statuses["disabled"]["status"], "disabled-by-user")
        self.assertEqual(holder.errors, [])
        self.assertIn("hermes-archive-context", (worker / "config.yaml").read_text(encoding="utf-8"))

    def directory_link(self, alias, target):
        import subprocess
        alias.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(target)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(alias.is_junction())
        else:
            alias.symlink_to(target, target_is_directory=True)
        self.directory_links = getattr(self, "directory_links", []) + [alias]

    def test_linked_profile_is_not_collected_or_provisioned(self):
        from unittest.mock import patch
        archive = load_archive()
        outside = self.root / "outside"
        seed(outside, "outside", "linked")
        (outside / "config.yaml").write_text("{}", encoding="utf-8")
        self.directory_link(self.home / "profiles/linked", outside)
        self.cfg["bootstrap_profiles"] = True
        archive.native_helpers(self.cfg)
        with patch.object(archive.subprocess, "run") as cli:
            result = archive.run(self.cfg)
        self.assertEqual(result["sessions"], 1)
        cli.assert_not_called()
        self.assertFalse((outside / "plugins").exists())
        self.assertFalse((self.vault / "30_Profiles/linked").exists())

    def test_bootstrap_rejects_linked_destinations_before_any_write(self):
        from unittest.mock import patch
        archive = load_archive()
        holder = archive.Archive(self.cfg)
        for relative in ("plugins", "skills", "plugins/hermes-archive-context", "skills/note-taking"):
            with self.subTest(relative=relative):
                worker = self.home / "profiles" / relative.replace("/", "-").replace(".", "-")
                worker.mkdir(parents=True)
                outside = self.root / worker.name
                outside.mkdir()
                (worker / "config.yaml").write_text("{}", encoding="utf-8")
                alias = worker / relative
                self.directory_link(alias, outside)
                with patch.object(holder, "atomic_write") as write, patch.object(archive.subprocess, "run") as cli:
                    with self.assertRaisesRegex((ValueError, RuntimeError), "[Ll]ink|escape"):
                        archive.bootstrap_profile(holder, worker.name, worker)
                write.assert_not_called()
                cli.assert_not_called()
                self.assertEqual(list(outside.iterdir()), [])

    def test_linked_runtime_rejected_before_checkpoint_writes(self):
        archive = load_archive()
        outside = self.root / "outside"
        outside.mkdir()
        self.directory_link(Path(self.cfg["runtime"]), outside)
        with self.assertRaisesRegex((ValueError, RuntimeError), "[Ll]ink|escape"):
            archive.Archive(self.cfg)
        self.assertEqual(list(outside.iterdir()), [])

    def test_minimal_profile_unset_optional_keys_preserves_retention_state(self):
        import subprocess
        from unittest.mock import patch
        archive = load_archive()
        holder = archive.Archive(self.cfg)
        holder.cfg["hermes_command"] = "mock-hermes"
        worker = self.home / "profiles/minimal"
        worker.mkdir(parents=True)
        (worker / "config.yaml").write_text("{}", encoding="utf-8")
        prune = "unset"
        calls = []
        def cli(command, **kwargs):
            nonlocal prune
            args = command[1:]
            calls.append(args)
            self.assertEqual(kwargs["env"]["HERMES_HOME"], str(worker.resolve()))
            if args[:2] == ["config", "get"]:
                if args[2] == "plugins.disabled" or prune == "unset":
                    return subprocess.CompletedProcess(command, 1, "", f"Config key not set: {args[2]}\n")
                return subprocess.CompletedProcess(command, 0, prune, "")
            if args[:2] == ["config", "set"]:
                prune = args[3]
            output = '[{"name":"hermes-archive-context","status":"enabled"}]' if args[:2] == ["plugins", "list"] else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        with patch.object(archive.subprocess, "run", side_effect=cli):
            archive.bootstrap_profile(holder, "minimal", worker)
            self.assertEqual(holder.state["bootstrap"]["minimal"]["previous_auto_prune"], "unset")
            self.assertEqual(prune, "false")
            holder.state["bootstrap"]["minimal"].pop("config_stamp")
            calls.clear()
            archive.bootstrap_profile(holder, "minimal", worker)
        self.assertEqual(holder.state["bootstrap"]["minimal"]["previous_auto_prune"], "unset")
        self.assertFalse(any(args[:2] == ["config", "set"] for args in calls))
        saved = json.loads(holder.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["bootstrap"]["minimal"]["previous_auto_prune"], "unset")

    def test_unexpected_cli_failures_remain_fatal_with_diagnostics(self):
        import subprocess
        from unittest.mock import patch
        archive = load_archive()
        holder = archive.Archive(self.cfg)
        holder.cfg["hermes_command"] = "mock-hermes"
        for code, output in ((2, "Config key not set: plugins.disabled"),
                             (1, "unexpected failure password=syntheticCliSecret"),
                             (1, "Config key not set: other.key")):
            with self.subTest(code=code, output=output):
                worker = self.home / "profiles" / f"failure-{code}-{len(output)}"
                worker.mkdir(parents=True)
                (worker / "config.yaml").write_text("{}", encoding="utf-8")
                response = subprocess.CompletedProcess([], code, "", output)
                with patch.object(archive.subprocess, "run", return_value=response) as cli:
                    with self.assertRaisesRegex(RuntimeError, f"exit {code}") as raised:
                        archive.bootstrap_profile(holder, worker.name, worker)
                self.assertIn("config get plugins.disabled", str(raised.exception))
                self.assertNotIn("syntheticCliSecret", str(raised.exception))
                self.assertEqual(cli.call_count, 1)
                self.assertNotIn("verified", holder.state["bootstrap"][worker.name])

    def test_temporary_symlink_cannot_write_outside_archive(self):
        archive = load_archive()
        holder = archive.Archive(self.cfg)
        (self.vault / "60_Operations").mkdir(parents=True)
        outside = self.root / "outside.txt"
        outside.write_text("preserve", encoding="utf-8")
        temporary = self.vault / "60_Operations/test.md.archive-tmp"
        from contextlib import nullcontext
        from unittest.mock import patch
        guard = nullcontext()
        try:
            temporary.symlink_to(outside)
        except OSError:
            # Windows without symlink privilege: exercise the same OS predicate boundary.
            guard = patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == temporary)
        with guard:
            with self.assertRaises(ValueError):
                holder.write("60_Operations/test.md", "replacement")
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
            with self.assertRaises(ValueError):
                holder.atomic_write(temporary.with_name("test.md"), b"replacement")
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_nested_folder_excludes_vault_configuration_artifacts(self):
        archive = load_archive()
        cfg = {**self.cfg, "vault_path": str(self.vault.parent), "archive_folder": "AI/Hermes"}
        cfg.pop("archive_root")
        holder = archive.Archive(cfg)
        sensitive = self.vault.parent / ".obsidian/private-config.json"
        self.assertEqual(holder.artifact_paths([{"role": "tool", "content": json.dumps({"path": str(sensitive)})}]), [])

    def test_session_roundtrip(self):
        archive = load_archive()
        first = archive.run(self.cfg)
        self.assertEqual(first["sessions"], 1)
        self.assertEqual(first["errors"], [])
        self.assertTrue((self.vault / "60_Operations/10_Record-Policy.md").exists())
        docs = list((self.vault / "40_Sessions").rglob("*.md"))
        text = "\n".join(p.read_text(encoding="utf-8") for p in docs)
        self.assertIn("테스트 요청", text)
        self.assertIn("synthetic failure", text)
        self.assertNotIn("OnlySyntheticSecretForArchiveTest", text)
        self.assertNotIn("INTERNAL_REASONING_MUST_NOT_EXPORT", text)
        self.assertIn("m-1", text)
        before = {p: p.stat().st_mtime_ns for p in docs}
        second = archive.run(self.cfg)
        self.assertEqual(second["sessions_written"], 0)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in docs})
        self.assertEqual(second["failures"], 1)


    def test_profiles_groups_and_project_links(self):
        archive = load_archive()
        other = self.home / "profiles" / "worker"
        seed(other, "s2", "worker")
        work = self.root / "project"
        work.mkdir()
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.execute("UPDATE sessions SET cwd=?", (str(work),))
        with closing(sqlite3.connect(self.home / "projects.db")) as db, db:
            db.executescript("CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,primary_path TEXT); CREATE TABLE project_folders(project_id TEXT,path TEXT);")
            db.execute("INSERT INTO projects VALUES(?,?,?)", ("p1", "테스트 프로젝트", str(work)))
            db.execute("INSERT INTO project_folders VALUES(?,?)", ("p1", str(work)))
        with closing(sqlite3.connect(self.home / "shared-state.db")) as db, db:
            db.executescript("""
                CREATE TABLE hosted_rooms(room_id TEXT PRIMARY KEY,name TEXT,members_json TEXT,created_at REAL,updated_at REAL);
                CREATE TABLE hosted_room_events(room_id TEXT,seq INTEGER,event_id TEXT,kind TEXT,actor_json TEXT,payload_json TEXT,created_at REAL);
                CREATE TABLE hosted_room_policy_events(room_id TEXT,seq INTEGER,event_json TEXT);
                CREATE TABLE hosted_room_remote_runs(room_id TEXT,target_profile TEXT,session_id TEXT,member_id TEXT,task_id TEXT);
            """)
            db.execute("INSERT INTO hosted_rooms VALUES(?,?,?,?,?)", ("room1", "합성 그룹", '[]', time.time(), time.time()))
            db.execute("INSERT INTO hosted_room_events VALUES(?,?,?,?,?,?,?)", ("room1",1,"e1","message.user",'{}','{"text":"공용 그룹 요청"}',time.time()))
            db.execute("INSERT INTO hosted_room_policy_events VALUES(?,?,?)", ("room1",2,'{"kind":"member.result","content":"정책 드라이버 결과"}'))
            db.execute("INSERT INTO hosted_room_policy_events VALUES(?,?,?)", ("room1",1,'{"event_id":"e1","kind":"message.user","payload":{"text":"공용 그룹 요청"}}'))
            db.execute("INSERT INTO hosted_room_remote_runs VALUES(?,?,?,?,?)", ("room1", "worker", "s2", "member1", "task1"))
        result = archive.run(self.cfg)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["sessions"], 2)
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["projects"], 1)
        group_text = "\n".join(p.read_text(encoding="utf-8") for p in (self.vault / "20_Group-Chats").rglob("*.md"))
        self.assertEqual(group_text.count("공용 그룹 요청"), 1)
        self.assertIn("정책 드라이버 결과", group_text)
        self.assertIn("합성 검사 - worker", group_text)
        self.assertTrue((self.vault / "30_Profiles/worker/00_Profile.md").exists())
        project_text = "\n".join(p.read_text(encoding="utf-8") for p in (self.vault / "10_Projects").rglob("*.md"))
        self.assertIn("합성 검사]]", project_text)
        self.assertTrue((self.vault / "00_Home.md").exists())
        self.assertTrue((self.vault / "60_Operations/00_Archive-Health.md").exists())


    def test_current_summary_template_preserves_authored_history_on_repeat(self):
        archive = load_archive()
        with closing(sqlite3.connect(self.home / "projects.db")) as db, db:
            db.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,primary_path TEXT)")
            db.execute("INSERT INTO projects VALUES(?,?,?)", ("p1", "Summary fixture", str(self.root / "project")))
        result = archive.run(self.cfg)
        self.assertEqual(result["errors"], [])
        current = next((self.vault / "10_Projects").glob("*/30_Current-State.md"))
        text = current.read_text(encoding="utf-8")
        self.assertIn("## 현재 요약", text)
        self.assertIn("## 작업 이력", text)
        self.assertLess(text.index("## 현재 요약"), text.index("## 작업 이력"))
        for field in ("현재 목표", "현재 단계", "확인된 결과", "남은 작업·차단", "다음 행동", "근거", "마지막 갱신"):
            self.assertIn("| " + field + " |", text)
        self.assertIn("미정리", text)
        self.assertIn("해당 없음", text)
        template = self.vault / "90_Templates/Current-State.md"
        self.assertEqual(template.read_text(encoding="utf-8"), text)
        custom = text.replace("| 현재 목표 | 미정리 |", "| 현재 목표 | 사용자 작성 목표 |") + "\n기존 작업 이력 보존\n"
        current.write_text(custom, encoding="utf-8")
        template.write_text("사용자 템플릿", encoding="utf-8")
        result = archive.run(self.cfg)
        self.assertEqual(result["errors"], [])
        self.assertEqual(current.read_text(encoding="utf-8"), custom)
        self.assertEqual(template.read_text(encoding="utf-8"), "사용자 템플릿")

    def test_artifacts_are_paths_not_unearned_validation(self):
        archive = load_archive()
        work = self.root / "project"
        (work / "source").mkdir(parents=True)
        result_file = work / "report.md"
        result_file.write_text("synthetic artifact", encoding="utf-8")
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.execute("UPDATE sessions SET cwd=?", (str(work),))
            db.execute("UPDATE messages SET tool_name='write_file',content=? WHERE id=3", (json.dumps({"resolved_path": str(result_file), "verified": True}),))
        archive.run(self.cfg)
        state = json.loads((Path(self.cfg["runtime"]) / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["sessions"]["default:s1"]["artifacts"][0]["path"], str(result_file))
        self.assertNotEqual(state["sessions"]["default:s1"]["artifacts"][0].get("status"), "validated")


    def test_interrupted_write_recovers_but_manual_edits_are_preserved(self):
        archive = load_archive()
        first = archive.Archive(self.cfg)
        first.write("60_Operations/00_Test.md", "initial generated content")
        # Simulate termination after verified file write, before checkpoint save.
        resumed = archive.Archive(self.cfg)
        resumed.write("60_Operations/00_Test.md", "updated generated content")
        resumed.save_state()
        path = self.vault / "60_Operations/00_Test.md"
        path.write_text("manual correction", encoding="utf-8")
        protected = archive.Archive(self.cfg)
        with self.assertRaisesRegex(RuntimeError, "Manual edit conflict"):
            protected.write("60_Operations/00_Test.md", "do not overwrite the user")
        self.assertEqual(path.read_text(encoding="utf-8"), "manual correction")


    def test_context_plugin_guidance_is_static_and_bounded(self):
        plugin = SOURCE / "archive-profile/__init__.py"
        self.assertTrue(plugin.exists(), "Archive context plugin is missing")
        deployed = self.root / "plugin/__init__.py"
        deployed.parent.mkdir()
        shutil.copyfile(plugin, deployed)
        (deployed.parent / "SKILL.md").write_text("synthetic skill", encoding="utf-8")
        seen = []
        class Context:
            def register_system_prompt_section(self, section_id, content, **kwargs):
                seen.append((section_id, content, kwargs))
            def register_skill(self, *args, **kwargs):
                pass
            def register_cli_command(self, *args, **kwargs):
                pass
        spec = importlib.util.spec_from_file_location("archive_context_check", deployed)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.register(Context())
        self.assertEqual(seen, [], "No settings must not guess a Vault")
        (deployed.parent / "archive-settings.json").write_text(json.dumps({
            **self.cfg, "vault_path": str(self.vault.parent), "archive_folder": "Hermes"}), encoding="utf-8")
        module.register(Context())
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0][1], str)
        self.assertLessEqual(len(seen[0][1]), seen[0][2]["max_chars"])
        self.assertIn("10_Requirements", seen[0][1])
        self.assertIn("30_Current-State", seen[0][1])


    def test_new_profile_is_provisioned_without_overwriting_user_files(self):
        archive = load_archive()
        self.assertTrue(hasattr(archive, "bootstrap_profiles"), "New Profile provisioning is missing")
        import shutil
        self.assertTrue(shutil.which("hermes"), "Real Hermes CLI required")
        worker = self.home / "profiles/worker"
        worker.mkdir(parents=True)
        (worker / "config.yaml").write_text("plugins:\n  enabled: []\n  disabled: []\n", encoding="utf-8")
        holder = archive.Archive(self.cfg)
        statuses = archive.bootstrap_profiles(holder, [("worker", worker)])
        self.assertEqual(statuses["worker"]["status"], "enabled")
        self.assertTrue((worker / "skills/note-taking/hermes-archive-records/SKILL.md").exists())
        self.assertTrue((worker / "plugins/hermes-archive-context/__init__.py").exists())
        self.assertTrue((worker / "plugins/hermes-archive-context/SKILL.md").exists())
        settings = json.loads((worker / "plugins/hermes-archive-context/archive-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(set(settings), {"base_home", "archive_root", "vault_path", "archive_folder", "runtime", "source"})
        self.assertEqual(settings["archive_root"], str(self.vault.resolve()))
        deployed = worker / "plugins/hermes-archive-context/__init__.py"
        deployed.write_text("# user-owned modification", encoding="utf-8")
        good = self.home / "profiles/good"
        good.mkdir(parents=True)
        (good / "config.yaml").write_text("plugins:\n  enabled: []\n  disabled: []\n", encoding="utf-8")
        statuses = archive.bootstrap_profiles(holder, [("worker", worker), ("good", good)])
        self.assertEqual(statuses["worker"]["status"], "error")
        self.assertEqual(statuses["good"]["status"], "enabled")
        self.assertTrue(any("modified" in error for error in holder.errors))
        self.assertEqual(deployed.read_text(encoding="utf-8"), "# user-owned modification")


    def test_history_titles_cannot_enable_active_markdown(self):
        archive = load_archive()
        title = "Danger\n\n```dataviewjs\nmalicious_marker\n```"
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.execute("UPDATE sessions SET title=?", (title,))
        result = archive.run(self.cfg)
        self.assertEqual(result["errors"], [])
        for path in self.vault.rglob("*.md"):
            self.assertNotIn("\n```dataviewjs\nmalicious_marker", path.read_text(encoding="utf-8"), str(path))


    def test_incomplete_journal_tail_does_not_block_recovery(self):
        archive = load_archive()
        first = archive.Archive(self.cfg)
        first.write("60_Operations/00_Tail.md", "verified before interruption")
        with first.journal.open("ab") as file:
            file.write(b'{"path":')
        resumed = archive.Archive(self.cfg)
        resumed.write("60_Operations/00_Tail.md", "recovered")
        resumed.save_state()
        self.assertEqual((self.vault / "60_Operations/00_Tail.md").read_text(encoding="utf-8"), "recovered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
