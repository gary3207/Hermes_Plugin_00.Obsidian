"""Real-CLI reinstall verification in disposable homes; never schedules a task.

Local: python source/verify_reinstall.py --bundle source/.publish-candidate
Remote: python source/verify_reinstall.py --repo owner/repo/source --ref <full SHA>
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from setup_archive import PAYLOAD, default_home, launcher
from test_archive import seed


def run(command, home, timeout=180):
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    if command[1:3] == ["plugins", "install"]:
        # Use existing gh authentication for this child only; do not alter global Git settings.
        env.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "", "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": "!gh auth git-credential"})
    result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        diagnostic = home / "archive-obsidian/setup-errors.jsonl"
        detail = diagnostic.read_text(encoding="utf-8") if diagnostic.is_file() else ""
        raise RuntimeError(f"Isolated command failed ({result.returncode}): {command[:2]}\n{result.stdout}\n{result.stderr}\n{detail}")
    return result.stdout


def initialize(home):
    home.mkdir()
    home.joinpath("config.yaml").write_text("plugins:\n  enabled: []\n  disabled: []\nsessions:\n  auto_prune: true\n", encoding="utf-8")


def configure(home, vault, bundle, command, repo, sha):
    if repo:
        run([command, "plugins", "install", repo, "--ref", sha, "--enable"], home, 360)
        payload = home / "plugins/hermes-archive-context"
    else:
        payload = bundle
    run([sys.executable, str(payload / "setup_archive.py"), "--vault", str(vault), "--folder", "AI/Work", "--home", str(home), "--no-schedule"], home)
    installed = home / "plugins/hermes-archive-context"
    run([command, "obsidian-archive", "--help"], home)
    settings = json.loads((installed / "archive-settings.json").read_text(encoding="utf-8"))
    assert settings["archive_folder"] == "AI/Work"
    assert settings["archive_root"] == str((vault / "AI/Work").resolve())
    return installed


def verify(bundle, repo=None, sha=None):
    if repo and (not sha or not re.fullmatch(r"[0-9a-f]{40}", sha)):
        raise ValueError("Remote verification requires an exact immutable SHA")
    command = launcher()
    # Disposable homes stay in Hermes-managed scratch, outside project repositories.
    scratch = default_home() / "cache/scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="obsidian-reinstall-", dir=scratch) as temp:
        root = Path(temp)
        if not repo:
            provided_bundle, bundle = bundle, root / "bundle"
            for name in PAYLOAD:
                destination = bundle / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(provided_bundle / name, destination)
        vault = root / "vault"
        vault.mkdir()
        first, second = root / "first-home", root / "reinstalled-home"
        initialize(first)
        seed(first, "reinstall-synthetic")
        project = root / "project"
        project.mkdir()
        with closing(sqlite3.connect(first / "state.db")) as db, db:
            db.execute("UPDATE sessions SET cwd=?", (str(project),))
        with closing(sqlite3.connect(first / "projects.db")) as db, db:
            db.executescript("CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,primary_path TEXT); CREATE TABLE project_folders(project_id TEXT,path TEXT);")
            db.execute("INSERT INTO projects VALUES(?,?,?)", ("synthetic-project", "Reinstall fixture", str(project)))
        installed = configure(first, vault, bundle, command, repo, sha)
        archive = vault / "AI/Work"
        health = archive / "60_Operations/00_Archive-Health.md"
        ledger = archive / "60_Operations/.archive-state.json"
        portable = json.loads(ledger.read_text(encoding="utf-8"))
        assert len(portable["sessions"]) == 1 and len(portable["projects"]) == 1
        text = health.read_text(encoding="utf-8")
        assert "errors: 0" in text
        assert 'FROM "AI/Work/40_Sessions"' in (archive / "00_Home.md").read_text(encoding="utf-8")
        human = next((archive / "10_Projects").glob("*/10_Requirements.md"))
        human.write_text(human.read_text(encoding="utf-8") + "\nPreserve my authored requirement.\n", encoding="utf-8")
        human_hash = hashlib.sha256(human.read_bytes()).hexdigest()
        conversations = {str(p.relative_to(archive)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (archive / "40_Sessions").rglob("*.md")}
        assert conversations
        for name in PAYLOAD:
            if not repo:
                assert (installed / name).read_bytes() == (bundle / name).read_bytes()
        shutil.rmtree(first)  # Only this verifier's newly created synthetic home.
        initialize(second)
        configure(second, vault, bundle, command, repo, sha)
        recovered = json.loads((second / "archive-obsidian/state.json").read_text(encoding="utf-8"))
        assert len(recovered["sessions"]) == 1 and len(recovered["projects"]) == 1
        assert hashlib.sha256(human.read_bytes()).hexdigest() == human_hash
        assert all(hashlib.sha256((archive / p).read_bytes()).hexdigest() == hash_value for p, hash_value in conversations.items())
        assert "reinstall-synthetic" in (archive / "00_Home.md").read_text(encoding="utf-8")
        mapping = (archive / "60_Operations/20_Project-Map.md").read_text(encoding="utf-8")
        assert "synthetic-project" in mapping
        assert not (second / "state.db").exists(), "Reinstall check must not rely on an original session DB"
        before_uninstall = {str(p.relative_to(archive)): hashlib.sha256(p.read_bytes()).hexdigest() for p in archive.rglob("*") if p.is_file()}
        run([sys.executable, str(second / "plugins/hermes-archive-context/setup_archive.py"), "--uninstall", "--home", str(second), "--no-schedule"], second)
        assert all(hashlib.sha256((archive / p).read_bytes()).hexdigest() == h for p, h in before_uninstall.items())
        disabled = run([command, "config", "get", "plugins.disabled"], second)
        assert "hermes-archive-context" in disabled
        return {"mode": "remote-pinned" if repo else "local-bundle", "commit": sha, "recovered_sessions": len(recovered["sessions"]), "recovered_projects": len(recovered["projects"]), "conversation_files_preserved": len(conversations), "authored_note_preserved": True, "uninstall_preserved_vault": True, "scheduled_tasks_created": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).parent / ".publish-candidate")
    parser.add_argument("--repo")
    parser.add_argument("--ref")
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle.resolve(), args.repo, args.ref), indent=2))
