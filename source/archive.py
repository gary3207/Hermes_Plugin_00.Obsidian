"""Read-only Hermes -> Obsidian archive. No network or LLM calls.

Run: python source/archive.py --config source/archive-config.json
Generated files are reconciled; user-authored reference notes are never replaced.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import re
import importlib
import shutil
import sqlite3
import subprocess
import sys
import time

VERSION = "1"
KST = timezone(timedelta(hours=9))
CURRENT_STATE_BODY = """## 현재 요약

| 항목 | 현재 내용 |
|---|---|
| 현재 목표 | 미정리 |
| 현재 단계 | 미정리 — 검토 / 구현 / 테스트 / 설치 / 운영 확인을 구분 |
| 확인된 결과 | 미정리 — 실제 확인한 사실만 |
| 남은 작업·차단 | 미정리 |
| 다음 행동 | 미정리 |
| 근거 | 미정리 — 세션 ID·링크 또는 관련 문서 |
| 마지막 갱신 | 미정리 — 요약을 갱신한 날짜·시각 |

> 미정리는 미실행이나 실패를 뜻하지 않습니다. 확인 후 해당 없음 / 미실행 / 미확인 / 미정리를 구분하세요.
> 완료·실패·차단·취소·요구/결정 변경·인수인계·긴 작업의 주요 단계 완료 등 의미 있는 전환에서 변경된 사실만 갱신합니다.
> 도구 호출마다 또는 주기적으로 다시 쓰지 않습니다. 과거 원문을 복제하지 않고 근거 링크를 남깁니다.

## 작업 이력

날짜별 완료·실제 검증·실패/차단·다음 작업을 보존합니다. 기존 이력은 삭제하지 않습니다.
"""
MESSAGE_FIELDS = ("id", "session_id", "role", "content", "tool_call_id", "tool_calls",
                  "tool_name", "timestamp", "active", "compacted", "display_kind",
                  "_compressed_summary", "display_order", "message_uid", "finish_reason")
SESSION_FIELDS = ("id", "title", "source", "cwd", "profile_name", "parent_session_id",
                  "started_at", "ended_at", "end_reason", "last_activity_at", "model",
                  "chat_id", "chat_type", "thread_id", "message_count", "tool_call_count", "archived")
# Kept out of SESSION_FIELDS so 10_Conversation.md front matter stays unchanged.
USAGE_FIELDS = ("model_config", "billing_provider", "api_call_count", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd")


def usage(session):
    """Model/effort/token facts for the summary; model_config is reduced to effort only."""
    try:
        effort = (json.loads(session.get("model_config") or "{}").get("reasoning_config") or {}).get("effort")
    except (ValueError, AttributeError):
        effort = None
    out = {"model": session.get("model"), "effort": effort, "provider": session.get("billing_provider")}
    out.update({k: session.get(k) for k in USAGE_FIELDS[2:]})
    return out


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def component(value):
    raw = str(value)
    name = re.sub(r"[^\w.-]+", "-", raw).strip(". -")[:90] or "unnamed"
    if name != raw or name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name += "--" + digest(raw)[:10]
    return name


def readable(text, limit=40):
    """Human file-name part: drops Windows/Obsidian-link/ownership-unsafe characters, keeps spaces and Korean."""
    text = re.sub(r"[\x00-\x1f\x7f\\/:*?\"<>|\[\]#^'`%{}]+", " ", html.unescape(str(text)))
    # shortcut: 40-char cap keeps nested subagent paths under Windows MAX_PATH for typical Vault roots.
    return " ".join(text.split())[:limit].strip(" .") or "제목 없음"


def date(value):
    try:
        return datetime.fromtimestamp(float(value), KST) if 0 <= float(value) <= 4_200_000_000 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def stamp(value):
    dt = date(value)
    return dt.isoformat(timespec="seconds") if dt else "unknown"


def fenced(text):
    # Historical text is data, never executable DataviewJS/Templater or live HTML.
    longest = max((len(x) for x in re.findall(r"`+", str(text))), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def inline(value):
    text = re.sub(r"[\x00-\x1f]+", " ", str(value))
    return html.escape(text).replace("[", "&#91;").replace("]", "&#93;").replace("|", " ")


def note(kind, title, body, **fields):
    meta = {"type": kind, "title": title, **fields}
    return "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) + "\n---\n\n# " + str(title).replace("\n", " ") + "\n\n" + body.rstrip() + "\n"


def link(path, label=None, prefix="Hermes"):
    name = str(path).replace("\\", "/").removesuffix(".md")
    return f"[[{prefix}/{name}{'|' + inline(label) if label else ''}]]"


def assert_managed_paths(home, paths):
    home = Path(os.path.abspath(home))
    if home.is_symlink() or home.is_junction() or home.resolve() != home:
        raise ValueError("Linked managed home; preserved")
    for path in paths:
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(home) or not path.resolve().is_relative_to(home):
            raise ValueError("Managed destination escapes home; preserved")
        for parent in (path, *path.parents):
            if parent.is_symlink() or parent.is_junction():
                raise ValueError("Linked managed destination; preserved")
            if parent == home:
                break


def normalize_config(cfg):
    cfg = dict(cfg)
    if cfg.get("runtime"):
        runtime = Path(cfg["runtime"])
        assert_managed_paths(runtime, [runtime / name for name in
                            ("state.json", "state.json.archive-tmp", "writes.jsonl", "collector.lock", "fatal.json")])
    if "vault_path" in cfg or "archive_folder" in cfg:
        if not cfg.get("vault_path") or "archive_folder" not in cfg:
            raise ValueError("vault_path and archive_folder must be supplied together")
        vault = Path(cfg["vault_path"]).resolve()
        folder = cfg["archive_folder"]
    elif cfg.get("archive_root"):
        legacy = Path(cfg["archive_root"])
        vault, folder = legacy.parent.resolve(), legacy.name
    else:
        raise ValueError("Archive location missing")
    if not isinstance(folder, str) or not folder or re.search(r'[\x00-\x1f\x7f\\|\[\]"\':<>?#`]', folder):
        raise ValueError("Unsafe vault-relative archive_folder")
    parts = folder.split("/")
    if any(p in {"", ".", ".."} or p != p.strip() or p.endswith(".") for p in parts):
        raise ValueError("Unsafe vault-relative archive_folder")
    if not vault.is_dir():
        raise FileNotFoundError(f"Vault does not exist: {vault}")
    root = (vault / folder).resolve()
    if root == vault or not root.is_relative_to(vault):
        raise ValueError("Archive folder escaped Vault")
    if cfg.get("archive_root") and Path(cfg["archive_root"]).resolve() != root:
        raise ValueError("Conflicting archive_root and Vault parameters")
    cfg.update(vault_path=str(vault), archive_folder=folder, archive_root=str(root))
    return cfg


def db_read(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def tables(db):
    return {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rows(db, table, fields=None, where="", args=(), order=""):
    actual = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
    wanted = [x for x in (fields or sorted(actual)) if x in actual]
    columns = ",".join('"' + x + '"' for x in wanted)
    return [dict(r) for r in db.execute(f'SELECT {columns} FROM "{table}" {where} {order}', args)]


def native_helpers(cfg):
    def imported():
        redactor = importlib.import_module("agent.redact").redact_sensitive_text
        renderer = importlib.import_module("hermes_cli.session_export_md").render_session_markdown
        return redactor, renderer
    try:
        return imported()
    except ImportError:
        pass
    candidates = []
    command = cfg.get("hermes_command") or shutil.which("hermes")
    if command:
        try:
            result = subprocess.run([command, "--version"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            match = re.search(r"^Install directory:\s*(.+)$", result.stdout, re.MULTILINE)
            if result.returncode == 0 and match:
                candidates.append(Path(match[1].strip()))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if cfg.get("native_workspace"):
        candidates.append(Path(cfg["native_workspace"]))
    candidates.extend(sorted(Path(cfg["base_home"]).glob("installs/*/environments/*/workspace"),
                             key=lambda p: p.stat().st_mtime, reverse=True))
    for workspace in candidates:
        if not all((workspace / filename).is_file() for filename in ("agent/redact.py", "hermes_cli/session_export_md.py")):
            continue
        sys.path.insert(0, str(workspace))
        try:
            return imported()
        except ImportError:
            sys.path.remove(str(workspace))
    raise RuntimeError("Hermes native exporter/redactor missing; refusing an unredacted export")


def json_value(text):
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def failure(value):
    obj = json_value(value)
    if isinstance(obj, dict):
        code = obj.get("exit_code")
        if obj.get("success") is False or obj.get("error") or (isinstance(code, int) and code != 0) or obj.get("status") in {"failed", "error", "denied"}:
            return str(obj.get("error") or obj.get("output") or f"exit_code={code}; status={obj.get('status')}")[:1200]
    return None


class Archive:
    def __init__(self, cfg):
        cfg = normalize_config(cfg)
        self.cfg = cfg
        self.root = Path(cfg["archive_root"])
        self.vault = Path(cfg["vault_path"])
        self.link = lambda path, label=None: link(path, label, cfg["archive_folder"])
        self.runtime = Path(cfg["runtime"])
        self.state_path = self.runtime / "state.json"
        self.mirror_path = self.output_path("60_Operations/.archive-state.json")
        mirror = json.loads(self.mirror_path.read_text(encoding="utf-8")) if self.mirror_path.exists() else None
        self.mirror_digest = digest(self.mirror_path.read_bytes()) if mirror is not None else None
        if self.mirror_path.exists():
            if not isinstance(mirror, dict) or mirror.get("owner") != "hermes-archive-context" or set(mirror) != {"owner", "version", "files", "sessions", "projects", "groups"}:
                raise RuntimeError("Foreign or malformed archive mirror; preserved")
            self.validate_state(mirror)
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else (mirror or {"version": VERSION, "files": {}, "sessions": {}})
        if self.state.get("archive_root") not in (None, str(self.root)):
            raise RuntimeError("Runtime archive root mismatch; preserve state")
        self.validate_state(self.state)
        if self.state.get("mirror_digest") is not None and self.state["mirror_digest"] != self.mirror_digest:
            raise RuntimeError("Mirror manual edit conflict; preserved")
        self.state["archive_root"] = str(self.root)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.journal = self.runtime / "writes.jsonl"
        if self.journal.exists():
            journal_bytes = self.journal.read_bytes()
            # A torn final record precedes the corresponding rename; retain only fsynced lines.
            boundary = journal_bytes.rfind(b"\n") + 1
            if boundary != len(journal_bytes):
                journal_bytes = journal_bytes[:boundary]
                self.journal.write_bytes(journal_bytes)
            for line in journal_bytes.decode("utf-8").splitlines():
                entry = json.loads(line)
                path = self.output_path(entry["path"])
                if path.is_file() and digest(path.read_bytes()) == entry["new"]:
                    self.state["files"][entry["path"]] = entry["new"]
        self.collector_digest = digest(Path(__file__).read_bytes())
        self.redactor, self.renderer = native_helpers(cfg)
        self.errors = []
        self.written = 0

    def output_path(self, relative):
        if not isinstance(relative, str):
            raise ValueError("Archive ownership path must be text")
        relative = relative.replace("\\", "/")
        if not relative or any(p in {"", ".", ".."} for p in relative.split("/")) or re.search(r'[\x00-\x1f\x7f|\[\]"\':<>?#`]', relative):
            raise ValueError("Unsafe archive ownership path")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Output escaped archive root")
        return path

    def validate_state(self, state):
        if not isinstance(state, dict) or state.get("version") != VERSION:
            raise RuntimeError("Archive state version mismatch or malformed state; preserved")
        for name in ("files", "sessions", "projects", "groups"):
            if not isinstance(state.get(name, {}), dict):
                raise ValueError("Malformed archive state section")
        for relative, hash_value in state.get("files", {}).items():
            self.output_path(relative)
            if relative == "60_Operations/.archive-state.json" or not isinstance(hash_value, str) or not re.fullmatch(r"[0-9a-f]{64}", hash_value):
                raise ValueError("Malformed ownership hash")
        required = {"sessions": {"path", "files", "profile", "id", "title", "failures", "source_rows"},
                    "projects": {"folder", "id", "name", "paths"}, "groups": {"folder", "name", "events"}}
        for name in required:
            for record in state.get(name, {}).values():
                if not isinstance(record, dict) or not required[name].issubset(record):
                    raise ValueError("Malformed archive metadata")
                self.output_path(record["path"] if name == "sessions" else record["folder"])
                if name == "sessions":
                    if not isinstance(record["files"], list) or not isinstance(record["failures"], list):
                        raise ValueError("Malformed session metadata")
                    for path in record["files"]:
                        self.output_path(path)
        if "files" not in state or "sessions" not in state:
            raise ValueError("Missing archive ownership metadata")

    def clean(self, value):
        if isinstance(value, str):
            value = self.redactor(value, force=True, redact_url_credentials=True)
            # Native redaction intentionally permits some inline prose assignments. Archive egress
            # is stricter: credentials must also be masked inside a user's sentence or quoted JSON.
            pattern = r'''(?i)((?:["']?)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|authorization|cookie|비밀번호)(?:["']?)\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}\]]+)'''
            value = re.sub(pattern, lambda m: m.group(1) + '"[REDACTED]"', value)
            return re.sub(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", value)
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            excluded = {"reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items", "system_prompt"}
            sensitive = {"password", "passwd", "api_key", "apikey", "access_token", "refresh_token", "client_secret", "authorization", "cookie", "cvc", "card_number", "verification_code"}
            return {k: ("[REDACTED]" if k.lower() in sensitive else self.clean(v)) for k, v in value.items() if k not in excluded}
        return value

    def write(self, relative, text, *, authored=False):
        relative = str(relative).replace("\\", "/")
        path = self.output_path(relative)
        if authored and path.exists():
            return False
        encoded = text.encode("utf-8")
        wanted = digest(encoded)
        if path.exists():
            current = digest(path.read_bytes())
            if current == wanted:
                self.state["files"][relative] = wanted
                return False
            previous = self.state["files"].get(relative)
            if previous != current:
                # ponytail: one writer; resolve manual edits rather than merging generated Markdown.
                raise RuntimeError(f"Manual edit conflict; preserved original: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".archive-tmp")
        if temporary.is_symlink() or temporary.is_junction():
            raise ValueError("Unsafe archive temporary link")
        try:
            # Write-ahead ownership, not a source checkpoint: recover a verified rename even if
            # the process stops before state.json commits. Raw conversation text never enters it.
            with self.journal.open("a", encoding="utf-8") as journal:
                journal.write(packed({"path": relative, "new": wanted}) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
            with temporary.open("wb") as fh:
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, path)
            if digest(path.read_bytes()) != wanted:
                raise OSError(f"Write verification failed: {relative}")
        finally:
            temporary.unlink(missing_ok=True)
        self.state["files"][relative] = wanted
        self.written += 1
        return True

    @staticmethod
    def atomic_write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".archive-tmp")
        if tmp.is_symlink() or tmp.is_junction():
            raise ValueError("Unsafe archive temporary link")
        try:
            with tmp.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            if digest(path.read_bytes()) != digest(data):
                raise OSError("Checkpoint verification failed")
        finally:
            tmp.unlink(missing_ok=True)

    def save_state(self):
        assert_managed_paths(self.runtime, [self.state_path, self.journal,
                             self.state_path.with_name("state.json.archive-tmp")])
        mirror_path = self.output_path("60_Operations/.archive-state.json")
        current = digest(mirror_path.read_bytes()) if mirror_path.exists() else None
        if current != self.mirror_digest:
            raise RuntimeError("Mirror manual edit conflict; preserved")
        fields = {
            "sessions": {"path", "profile", "id", "title", "cwd", "parent", "created", "last_activity", "source_rows", "archived_rows", "failures", "chat_id", "files", "artifacts", "collected_at", "project_id"},
            "projects": {"id", "name", "folder", "paths"},
            "groups": {"folder", "name", "events"},
        }
        mirror = {"owner": "hermes-archive-context", "version": VERSION, "files": dict(self.state["files"])}
        for name, allowed in fields.items():
            mirror[name] = {key: self.clean({k: v for k, v in record.items() if k in allowed})
                            for key, record in self.state.get(name, {}).items()}
        self.validate_state(mirror)
        data = packed(mirror).encode("utf-8")
        if digest(data) != current:
            self.atomic_write(mirror_path, data)
        self.mirror_digest = digest(data)
        self.state["mirror_digest"] = self.mirror_digest
        self.atomic_write(self.state_path, packed(self.state).encode("utf-8"))
        self.journal.unlink(missing_ok=True)

    def render(self, session, messages):
        data = {k: session.get(k) for k in SESSION_FIELDS}
        data["title"] = inline(self.clean(session.get("title") or session["id"]))
        rendered_messages = []
        for m in messages:
            raw = m.get("content") or ""
            decoded = json_value(raw)
            content = self.clean(decoded if isinstance(decoded, (dict, list)) else raw)
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            revision = "inactive/revision" if m.get("active", 1) == 0 else "active"
            rendered_messages.append({"role": m.get("role"), "tool_name": m.get("tool_name"),
                "timestamp": m.get("timestamp"),
                "content": f"#### Message m-{m['id']}\n\n- Record: `{revision}`; display: `{m.get('display_kind')}`\n\n" + fenced(content),
                **({"tool_calls": self.clean(json_value(m.get("tool_calls")) or m["tool_calls"])} if m.get("tool_calls") else {})})
        data["messages"] = rendered_messages
        text = self.renderer(data, include_verification=False)
        # Native renderer uses wall-clock export time; remove it for deterministic, no-op reruns.
        return re.sub(r"^exported_at:.*\n", "", text, flags=re.MULTILINE)

    def artifact_paths(self, messages):
        candidates = set()
        for m in messages:
            if m.get("role") == "tool":
                result = json_value(m.get("content"))
                if isinstance(result, dict):
                    for key in ("resolved_path", "path", "output_path", "file_path"):
                        if isinstance(result.get(key), str):
                            candidates.add(result[key])
                    for item in result.get("files_modified", []) or []:
                        if isinstance(item, str):
                            candidates.add(item)
            if m.get("role") == "assistant" and m.get("display_kind") != "hidden":
                candidates.update(re.findall(r"MEDIA:([^\r\n]+)", m.get("content") or ""))
        output = []
        excluded = [Path(self.cfg["base_home"]).resolve(), self.root, (self.vault / ".obsidian").resolve()]
        for raw in sorted(candidates):
            path = Path(raw.strip())
            if not path.is_absolute():
                continue
            path = path.resolve()
            if any(path.is_relative_to(root) for root in excluded):
                continue
            output.append({"path": str(path), "status": "exists" if path.is_file() else "reported", "validation": "not-assessed"})
        return output

    def parent_record(self, profile, parent_id):
        return self.state["sessions"].get(f"{profile}:{parent_id}") if parent_id else None

    def layout(self, key, profile, session, raw_title):
        """Summary, conversation, tool paths: '40_Sessions/YYYY-MM/YYYY-MM-DD HHMM 제목/', subagents under the parent."""
        dt = date(session.get("started_at"))
        name = readable(raw_title.removeprefix("Subagent: ")) + (f" - {readable(profile, 20)}" if profile != "default" else "")
        parent = self.parent_record(profile, session.get("parent_session_id"))
        taken = {s["path"].casefold() for k, s in self.state["sessions"].items() if k != key}
        for suffix in ("", f" ({str(session['id'])[-6:]})"):
            if parent:
                stem = (f"{dt:%H%M} " if dt else "") + name + suffix
                base = Path(parent["path"]).parent / "Subagents"
                names = (stem + ".md", stem + " - 대화.md", stem + " - 도구.md")
            else:
                stem = (f"{dt:%Y-%m-%d %H%M} " if dt else "") + name + suffix
                base = Path("40_Sessions") / (dt.strftime("%Y-%m") if dt else "unknown") / stem
                names = (stem + ".md", "대화.md", "도구 실행.md")
            paths = [str(base / n).replace("\\", "/") for n in names]
            if paths[0].casefold() not in taken:
                break
        return paths

    def retire(self, old_files, keep):
        """Remove a moved session's previous generated files only when still byte-identical to what we wrote."""
        for relative in old_files:
            if relative in keep:
                continue
            path = self.output_path(relative)
            if path.exists():
                if digest(path.read_bytes()) != self.state["files"].get(relative):
                    self.errors.append(f"Moved session; edited old file preserved: {relative}")
                    continue
                path.unlink()
            self.state["files"].pop(relative, None)
            self.prune_empty(path.parent)

    def prune_empty(self, folder):
        # Best-effort: OneDrive/Obsidian watchers can deny rmdir on an empty folder; it holds no data.
        for parent in (folder, *folder.parents):
            if parent == self.root or not parent.is_relative_to(self.root) or not parent.is_dir() or any(parent.iterdir()):
                break
            try:
                parent.rmdir()
            except OSError:
                break

    def session(self, profile, session, messages):
        key = f"{profile}:{session['id']}"
        allowed = [m for m in messages if m.get("role") in {"user", "assistant", "tool"} and m.get("display_kind") != "hidden"]
        public = [{**m, "tool_calls": None} for m in allowed if m.get("role") in {"user", "assistant"}]
        user = next((m.get("content") or "" for m in public if m["role"] == "user"), "사용자 Prompt 없음")
        raw_title = self.clean(session.get("title") or "")
        if not raw_title and any(m["role"] == "user" for m in public):
            raw_title = "(제목 없음) " + readable(self.clean(str(user)), 40)
        raw_title = raw_title or str(session["id"])
        paths = self.layout(key, profile, session, raw_title)
        content_hash = digest(packed({"session": session, "messages": messages}))
        old = self.state["sessions"].get(key, {})
        if old.get("collector_digest") == self.collector_digest and old.get("digest") == content_hash and old.get("path") == paths[0] and all((self.root / p).exists() for p in old.get("files", [])):
            for p in old["files"]:
                if digest((self.root / p).read_bytes()) != self.state["files"].get(p):
                    raise RuntimeError(f"Manual edit conflict; preserved original: {p}")
            return False
        tool = [m for m in allowed if m.get("role") == "tool" or m.get("tool_calls")]
        found = [{"message_id": m["id"], "tool": m.get("tool_name") or "assistant", "detail": self.clean(str(detail))} for m in allowed if (detail := failure(m.get("content")) if m.get("role") == "tool" else ("Failed assistant turn" if m.get("display_kind") == "failed_turn" else None))]
        title = inline(raw_title)
        last = next((m.get("content") or "" for m in reversed(public) if m["role"] == "assistant"), "공개 응답 없음")
        u = usage(session)
        body = (f"- Session ID: `{session['id']}`\n- Profile: `{profile}`\n- 시작: {stamp(session.get('started_at'))}\n"
                f"- 원본 행: {len(messages)}; 기록 행: {len(allowed)}; 숨김/시스템 등 제외: {len(messages)-len(allowed)}\n"
                f"- 원본 위치: `{session.get('cwd') or '미분류'}`\n- 부모 세션: `{session.get('parent_session_id') or '없음'}`\n"
                f"- 세션 상태: {'종료: ' + str(session.get('end_reason')) if session.get('ended_at') else '종료 미기록 (진행 중이라고 단정하지 않음)'}\n"
                f"- 모델: `{u['model'] or '미기록'}` · Effort: `{u['effort'] or '미기록'}` · Provider: `{u['provider'] or '미기록'}` · API 호출: {u['api_call_count'] or 0}\n"
                f"- Token: 입력 {u['input_tokens'] or 0:,} · 출력 {u['output_tokens'] or 0:,} · Reasoning {u['reasoning_tokens'] or 0:,} · Cache 읽기 {u['cache_read_tokens'] or 0:,} · Cache 쓰기 {u['cache_write_tokens'] or 0:,} · 추정 비용 ${u['estimated_cost_usd'] or 0:.4f} (구독 Provider는 0으로 표시될 수 있음)\n"
                f"- {self.link(paths[1], '사용자 Prompt·공개 응답')}\n"
                + (f"- {self.link(paths[2], '도구 실행·검증 근거')}\n" if tool else "")
                + "\n## 최초 요청 발췌\n\n" + fenced(self.clean(str(user))[:1600])
                + "\n\n## 최근 Agent 응답 발췌\n\n> 응답 발췌는 완료 판정·내용 검증이 아닙니다. 전체 원문을 확인하세요.\n\n" + fenced(self.clean(str(last))[:2000])
                + "\n\n## 실패·차단 기록\n\n")
        body += "\n".join(f"- m-{f['message_id']} · {f['tool']} · {self.link(paths[2] if f['tool'] != 'assistant' else paths[1])}\n\n{fenced(f['detail'])}" for f in found) or "구조화된 오류로 확인된 항목 없음. 자유문장의 오류는 자동 판정하지 않습니다."
        text = note("session", title, body, session_id=session["id"], profile=profile, created=stamp(session.get("started_at")), failures=len(found), status="ended" if session.get("ended_at") else "unfinalized", archived_rows=len(allowed), source_rows=len(messages), parent_session=session.get("parent_session_id"), **{k: u[k] for k in ("model", "effort", "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "estimated_cost_usd")}, generated=True)
        outputs = {paths[0]: text, paths[1]: self.render(session, public)}
        if tool:
            outputs[paths[2]] = self.render(session, tool)
        paths = list(outputs)
        changed = False
        for path, document in outputs.items():
            changed |= self.write(path, document)
        self.retire(old.get("files", []), paths)
        self.state["sessions"][key] = {"digest": content_hash, "collector_digest": self.collector_digest, "path": paths[0], "profile": profile,
            "id": session["id"], "title": title, "cwd": session.get("cwd"), "parent": session.get("parent_session_id"), "model": u["model"],
            "created": session.get("started_at"), "last_activity": session.get("last_activity_at"),
            "source_rows": len(messages), "archived_rows": len(allowed), "failures": found,
            "chat_id": session.get("chat_id"), "files": paths, "artifacts": self.artifact_paths(allowed),
            "collected_at": time.time() if changed else old.get("collected_at", time.time())}
        return changed


    def groups(self, base):
        source = Path(base) / "shared-state.db"
        self.state.setdefault("groups", {})
        if not source.exists():
            return
        with closing(db_read(source)) as db:
            available = tables(db)
            if "hosted_rooms" not in available:
                raise RuntimeError("Unsupported Group Chat schema")
            for room in rows(db, "hosted_rooms"):
                rid = room["room_id"]
                previous = self.state["groups"].get(rid, {})
                folder = Path(previous.get("folder") or f"20_Group-Chats/{component(room.get('name') or 'group')}--{component(rid)}")
                items = []
                if "hosted_room_events" in available:
                    for event in rows(db, "hosted_room_events", where="WHERE room_id=?", args=(rid,), order="ORDER BY seq"):
                        items.append({"namespace": "room", "id": event.get("event_id"), "seq": event["seq"],
                                      "kind": event.get("kind"), "timestamp": event.get("created_at"),
                                      "actor": json_value(event.get("actor_json")), "payload": json_value(event.get("payload_json")) or event.get("payload_json")})
                known_ids = {i.get("id") for i in items if i.get("id")}
                if "hosted_room_policy_events" in available:
                    for event in rows(db, "hosted_room_policy_events", where="WHERE room_id=?", args=(rid,), order="ORDER BY seq"):
                        data = json_value(event.get("event_json")) or {"content": event.get("event_json")}
                        if isinstance(data, dict) and data.get("event_id") in known_ids:
                            continue  # Policy projection of the canonical event, not a second message.
                        items.append({"namespace": "policy", "seq": event["seq"], "data": data,
                                      "timestamp": data.get("created_at") or data.get("timestamp") or room.get("created_at")})
                months = {}
                for event in items:
                    dt = date(event.get("timestamp"))
                    month = dt.strftime("%Y-%m") if dt else "unknown"
                    months.setdefault(month, []).append(event)
                for month, events in months.items():
                    body = "\n\n".join(f"## {e['namespace']} · seq-{e['seq']} · {stamp(e.get('timestamp'))}\n\n" + fenced(json.dumps(self.clean(e), ensure_ascii=False, indent=2)) for e in events)
                    self.write(folder / f"{month}.md", note("group-log", f"{self.clean(room.get('name') or rid)} · {month}", body, room_id=rid, events=len(events), generated=True))
                runs = rows(db, "hosted_room_remote_runs", ("room_id", "target_profile", "session_id", "member_id", "task_id", "execution_generation"), "WHERE room_id=?", (rid,)) if "hosted_room_remote_runs" in available else []
                connected = []
                for run_item in runs:
                    record = self.state["sessions"].get(f"{run_item.get('target_profile')}:{run_item.get('session_id')}")
                    connected.append(f"- {run_item.get('target_profile')} · `{run_item.get('session_id')}` · " + (self.link(record["path"]) if record else "세션 미수집/원격 호스트: 원격 수집기 필요"))
                for record in self.state["sessions"].values():
                    if record.get("chat_id") == rid and record["id"] not in {r.get("session_id") for r in runs}:
                        connected.append("- " + self.link(record["path"], record["title"]))
                body = f"- Room ID: `{rid}`\n- 공용 이벤트: {len(items)}\n- 해산: {stamp(room.get('disbanded_at')) if room.get('disbanded_at') else '미기록'}\n\n## 참여자\n\n" + fenced(json.dumps(self.clean(json_value(room.get("members_json")) or []), ensure_ascii=False, indent=2))
                body += "\n\n## 공용 기록\n\n" + ("\n".join("- " + self.link(folder / f"{month}.md", month) for month in sorted(months)) or "공용 이벤트 없음")
                body += "\n\n## 개별 봇 작업 세션\n\n" + ("\n".join(dict.fromkeys(connected)) or "연결된 개별 실행 없음")
                self.write(folder / "00_Overview.md", note("group", self.clean(room.get("name") or rid), body, room_id=rid, generated=True))
                self.state["groups"][rid] = {"folder": str(folder).replace("\\", "/"), "name": self.clean(room.get("name") or rid), "events": len(items)}

    def projects(self, base):
        source = Path(base) / "projects.db"
        self.state.setdefault("projects", {})
        projects, project_folders = [], []
        if source.exists():
            with closing(db_read(source)) as db:
                available = tables(db)
                if "projects" not in available:
                    raise RuntimeError("Unsupported projects.db schema")
                projects = rows(db, "projects")
                project_folders = rows(db, "project_folders") if "project_folders" in available else []
        for project in projects:
            pid = project["id"]
            previous = self.state["projects"].get(pid, {})
            folder = previous.get("folder") or f"10_Projects/{component(project.get('name') or pid)}--{component(pid)}"
            paths = list(dict.fromkeys([p for p in [project.get("primary_path"), *(f["path"] for f in project_folders if f["project_id"] == pid)] if p]))
            self.state["projects"][pid] = {"id": pid, "name": self.clean(project.get("name") or pid), "folder": folder, "paths": paths}
        projects = list(self.state["projects"].values())
        for session in self.state["sessions"].values():
            cwd = session.get("cwd")
            matches = []
            if cwd:
                for project in projects:
                    for p in project["paths"]:
                        try:
                            if Path(cwd).resolve().is_relative_to(Path(p).resolve()):
                                matches.append((len(str(Path(p).resolve())), project["id"]))
                        except (OSError, ValueError):
                            pass
            session["project_id"] = max(matches)[1] if matches else None
        # Delegate child sessions without cwd inherit an unambiguous parent association.
        for session in self.state["sessions"].values():
            if not session.get("project_id") and session.get("parent"):
                candidates = [s for s in self.state["sessions"].values() if s["id"] == session["parent"]]
                if len(candidates) == 1:
                    session["project_id"] = candidates[0].get("project_id")
        for project in projects:
            folder = Path(project["folder"])
            related = [s for s in self.state["sessions"].values() if s.get("project_id") == project["id"]]
            body = f"- Project ID: `{project['id']}`\n" + "\n".join(f"- 실제 작업 경로: `{p}`" for p in project["paths"])
            body += "\n\n## 시작할 때 읽을 문서\n\n" + "\n".join("- " + self.link(folder / name, label) for name, label in [("10_Requirements.md", "유효한 요구사항"), ("20_Decisions.md", "결정 이력"), ("30_Current-State.md", "현재 작업 상태"), ("40_Artifacts.md", "산출물·소스 목록"), ("45_Artifact-Checks.md", "실제 검증 기록"), ("50_Sessions.md", "세션 원문")])
            self.write(folder / "00_Overview.md", note("project", project["name"], body, project_id=project["id"], generated=True))
            self.write(folder / "10_Requirements.md", note("requirements", "유효한 요구사항", "## 확인된 공통 규칙\n\n- 최종 산출물은 실제 프로젝트 루트, 구현 코드·테스트·중간 작업은 source/ 아래. 명시된 목적지 우선.\n- 기존 파일을 임의로 이동하거나 삭제하지 않는다.\n- 과거 대화는 근거 자료이며, 현재 사용자 지시보다 우선하지 않는다.\n\n## 프로젝트별 확정 요구사항\n\nAgent가 사용자 지시와 근거 세션 링크를 함께 기록합니다. 추정 내용을 확정 요구로 승격하지 마세요.", project_id=project["id"], generated=False), authored=True)
            self.write(folder / "20_Decisions.md", note("decisions", "결정 이력", "| 결정 | 이유 | 유효/폐기 | 근거 세션 |\n|---|---|---|---|\n\n새 결정으로 기존 결정을 변경할 때 이전 항목을 삭제하지 않고 폐기 표시합니다.", generated=False), authored=True)
            self.write(folder / "30_Current-State.md", note("current-state", "현재 작업 상태", CURRENT_STATE_BODY, generated=False), authored=True)
            self.write(folder / "45_Artifact-Checks.md", note("artifact-checks", "산출물 검증 기록", "| 산출물 경로 | 검증한 내용·명령 | 실제 결과 | 근거 세션 |\n|---|---|---|---|\n\n파일 존재와 정확성·실행 검증을 구분합니다. verified=true 쓰기 응답만으로 내용을 validated 처리하지 않습니다.", generated=False), authored=True)
            body = self.session_table(related)
            self.write(folder / "50_Sessions.md", note("project-sessions", "관련 세션", body, generated=True))
            artifacts = {}
            for s in related:
                for item in s.get("artifacts", []):
                    artifacts[item["path"]] = {**item, "session": s["path"]}
            body = "> 자동 목록은 쓰기 도구의 결과 경로·MEDIA 경로 기준입니다. 직접 실행한 임의 스크립트의 파일을 모두 발견한다고 보장하지 않습니다. Agent는 누락 경로를 세션 또는 검증 기록에 명시하세요.\n\n"
            for item in artifacts.values():
                exists = Path(item["path"]).is_file()
                purpose = "구현·소스" if "source" in Path(item["path"]).parts else "산출물 후보"
                body += f"- `{item['path']}` · {purpose} · {'exists' if exists else 'missing'} · 내용 검증: 미판정 · {self.link(item['session'])}\n"
            self.write(folder / "40_Artifacts.md", note("artifacts", "산출물·소스 목록", body or "경로 미수집", generated=True))
        mapping = "\n\n".join(f"## {p['name']}\n\n" + "\n".join(f"- 실제 경로: `{x}`" for x in p["paths"]) + "\n- " + self.link(Path(p["folder"]) / "00_Overview.md") for p in projects)
        self.write("60_Operations/20_Project-Map.md", note("project-map", "작업 경로 → 프로젝트 기록", mapping or "등록 프로젝트 없음", generated=True))

    def top_level(self, records):
        """User-started sessions, newest first, plus subagent counts; subagents are listed under their parent instead."""
        kids = {}
        for s in self.state["sessions"].values():
            if self.parent_record(s["profile"], s.get("parent")):
                kids[f"{s['profile']}:{s['parent']}"] = kids.get(f"{s['profile']}:{s['parent']}", 0) + 1
        tops = [s for s in records if not self.parent_record(s["profile"], s.get("parent"))]
        return sorted(tops, key=lambda s: s.get("created") or 0, reverse=True), kids

    @staticmethod
    def when(s, fmt="%Y-%m-%d %H:%M"):
        dt = date(s.get("created"))
        return dt.strftime(fmt) if dt else "시간 미기록"

    def listing(self, records, limit=None, fmt="%Y-%m-%d %H:%M"):
        tops, kids = self.top_level(records)
        return "\n".join(f"- {self.when(s, fmt)} · {self.link(s['path'], s['title'])}"
                         + (f" · ↳ Subagent {kids[k]}" if (k := f"{s['profile']}:{s['id']}") in kids else "")
                         for s in tops[:limit]) or "세션 없음"

    def session_table(self, records):
        tops, kids = self.top_level(records)
        if not tops:
            return "연결된 세션 없음"
        # Escaped pipe keeps wikilink aliases intact inside Markdown tables.
        return "| 시작 | 세션 | 모델 | Subagent |\n|---|---|---|---|\n" + "\n".join(
            f"| {self.when(s)} | {self.link(s['path'], s['title']).replace('|', chr(92) + '|')} | {s.get('model') or '미기록'} | {kids.get(s['profile'] + ':' + s['id'], 0)} |"
            for s in tops)

    def indexes(self, homes, result):
        records = list(self.state["sessions"].values())
        tops, _ = self.top_level(records)
        days = {}
        for s in tops:
            if date(s.get("created")):
                days.setdefault(self.when(s, "%Y-%m-%d"), []).append(s)
        for day, items in days.items():
            self.write(f"40_Sessions/{day[:7]}/{day}.md", note("session-day", day, self.listing(items, fmt="%H:%M"), sessions=len(items), generated=True))
        base = f"""filters:
  and:
    - file.inFolder("{self.cfg['archive_folder']}/40_Sessions")
    - 'type == "session"'
views:
  - type: table
    name: 내가 시작한 세션
    filters:
      not:
        - parent_session
    order:
      - created
      - file.name
      - model
      - failures
    sort:
      - property: created
        direction: DESC
  - type: table
    name: Subagent 포함 전체
    order:
      - created
      - file.name
      - parent_session
      - model
    sort:
      - property: created
        direction: DESC
"""
        self.write("세션 찾기.base", base, authored=True)
        for profile, home in homes:
            related = [s for s in records if s["profile"] == profile]
            body = f"- Profile ID: `{profile}`\n- 실제 Profile 홈: `{home}`\n- 기록된 세션: {len(related)} (Subagent 포함)\n\n## 관련 세션\n\n"
            body += self.listing(related)
            self.write(f"30_Profiles/{component(profile)}/00_Profile.md", note("profile", profile, body, profile=profile, generated=True))
        failures = []
        for s in records:
            for f in s["failures"]:
                failures.append(f"## {s['title']} · m-{f['message_id']}\n\n- Profile: `{s['profile']}`\n- 근거: {self.link(s['path'])}\n- 상태: 분류 전 (해결 여부·원인은 자동 추정하지 않음)\n\n{fenced(f['detail'])}")
        self.write("50_Lessons/10_Failure-Inbox.md", note("failure-inbox", "실패·차단 분류함", "구조화된 도구 오류와 failed_turn 기록입니다. 해결된 재시도도 포함합니다. 교훈으로 승격하기 전에 원인·해결·재현을 확인하세요.\n\n" + ("\n\n".join(failures) or "기록된 구조화 오류 없음"), generated=True))
        self.write("50_Lessons/00_Index.md", note("lessons-index", "검증된 교훈", f"- [[{self.cfg['archive_folder']}/50_Lessons/10_Failure-Inbox|분류 전 실패 기록]]\n\n## 검증된 교훈\n\n재현 또는 실행 근거를 확인한 교훈만 여기에 연결합니다.\n\n## 적용 원칙\n\n적용 환경·조건이 맞는지 먼저 확인하고, 오래된 교훈은 현재 작업에서 다시 검증합니다.", generated=False), authored=True)
        home_body = "## 프로젝트\n\n" + ("\n".join("- " + self.link(Path(p["folder"]) / "00_Overview.md", p["name"]) for p in self.state.get("projects", {}).values()) or "등록 프로젝트 없음")
        home_body += "\n\n## Group Chat\n\n" + ("\n".join("- " + self.link(Path(g["folder"]) / "00_Overview.md", g["name"]) for g in self.state.get("groups", {}).values()) or "현재 수집 대상 DB에 Group Chat 없음")
        home_body += "\n\n## Profile\n\n" + "\n".join("- " + self.link(f"30_Profiles/{component(p)}/00_Profile.md", p) for p, _ in homes)
        home_body += "\n\n## 최근 세션\n\n" + f"- 검색·정렬: [[{self.cfg['archive_folder']}/세션 찾기.base|세션 찾기]] · 날짜별: " + (" · ".join(self.link(f"40_Sessions/{d[:7]}/{d}.md", d[5:]) for d in sorted(days, reverse=True)[:14]) or "없음") + "\n\n" + self.listing(records, 30)
        home_body += f"\n\n## 실패·운영 상태\n\n- [[{self.cfg['archive_folder']}/50_Lessons/00_Index|검증된 교훈]]\n- [[{self.cfg['archive_folder']}/50_Lessons/10_Failure-Inbox|실패·차단 분류함]]\n- [[{self.cfg['archive_folder']}/60_Operations/00_Archive-Health|수집 상태]]\n- [[{self.cfg['archive_folder']}/60_Operations/10_Record-Policy|기록·보안 정책]]\n\n## Dataview: 오류를 기록한 세션\n\n```dataview\nTABLE created AS 시작, profile AS 봇, failures AS 오류기록\nFROM \"{self.cfg['archive_folder']}/40_Sessions\"\nWHERE type = \"session\" AND failures > 0\nSORT created DESC\nLIMIT 30\n```\n\n> 상세 원문은 실행되지 않는 text 코드블록으로 보존됩니다. 오류기록 수는 미해결 오류 수가 아닙니다."
        self.write("00_Home.md", note("home", "Hermes 기록", home_body, generated=True))
        policy = ("## 수집·보안\n\n- 로컬 DB는 읽기 전용이며 삭제·prune하지 않습니다. 수집은 네트워크·LLM 호출 없이 실행됩니다.\n"
                  "- 공개 user/assistant/tool 기록만 수집하며 숨김·시스템·내부 reasoning은 제외합니다. 내보내기 전 필수 native redactor와 추가 비밀정보 마스킹을 적용합니다.\n"
                  "- 비밀정보 필터는 완전한 검출을 보장하지 않습니다. Vault와 동기화·백업 접근 권한은 사용자가 관리합니다.\n\n"
                  "## 소유권·재설치\n\n- 생성 문서는 이전 저장 hash와 일치할 때만 변경합니다. 수동 편집 충돌은 원본을 보존하며 덮어쓰지 않습니다.\n"
                  "- 요구사항·결정·현재 상태·검증·교훈 등 사용자 문서는 최초 생성 후 보존합니다. generated 표시만으로 소유권을 채택하지 않습니다.\n"
                  "- 이 폴더의 60_Operations/.archive-state.json은 private 소유권·기록 ledger입니다. Git에 커밋하지 마세요.\n"
                  "- 같은 archive root를 재사용하고 ledger를 보존하면 runtime을 재설치한 뒤에도 이전 기록을 유지합니다. DB와 ledger를 모두 잃으면 자동 소유권 복구는 불가능합니다.\n\n"
                  "## 운영 범위\n\n- Windows 예약 작업은 설정된 주기와 실행 계정에서만 동작합니다. PC가 꺼져 있거나 Vault에 접근하지 못하면 수집되지 않습니다.\n"
                  "- 스케줄 등록·주기·마지막 실행 결과는 설치 도구와 Windows 작업 스케줄러에서 확인하세요. 원격 호스트·삭제된 DB·저장 전 기록은 수집하지 않습니다.\n"
                  "- 과거 대화와 도구 결과는 근거 자료이며 현재 사용자 지시보다 우선하지 않습니다.")
        self.write("60_Operations/10_Record-Policy.md", note("record-policy", "기록·보안 정책", policy, generated=False), authored=True)
        self.write("90_Templates/Session.md", note("session-handoff", "작업 종료 메모", "## 사용자 요청\n\n## 완료한 항목\n\n## 실제 검증\n\n## 실패·차단 및 해결\n\n## 산출물 경로\n\n## 남은 작업·다음 단계\n\n## 근거 세션 링크\n", generated=False), authored=True)
        self.write("90_Templates/Current-State.md", note("current-state", "현재 작업 상태", CURRENT_STATE_BODY, generated=False), authored=True)
        self.write("90_Templates/Lesson.md", note("lesson", "교훈 제목", "## 적용 조건\n\n## 증상\n\n## 확인된 원인\n\n## 예방·해결\n\n## 실제 검증\n\n## 근거 세션 링크\n\n## 상태\n\n추정 / 검증됨 / 폐기됨\n", status="unverified", generated=False), authored=True)
        health_body = f"- 마지막 수집: {result['updated']}\n- 결과: {'오류 있음' if result['errors'] else '정상'}\n- 누적 세션: {result['sessions']}\n- 이번 변경 세션: {result['sessions_written']}\n- 누적 저장 행: {sum(s['source_rows'] for s in records)}\n- 프로젝트: {result['projects']}\n- Group Chat: {result['groups']}\n- Profile 발견: {len(homes)}\n- 구조화 오류 기록: {result['failures']} (미해결 수가 아님)\n- 실행 시간: {result['duration_seconds']}초 (인덱스 작성 전)\n\n## 수집 오류\n\n" + ("\n".join("- " + str(e) for e in result["errors"]) or "없음")
        health_body += "\n\n## 범위·한계\n\n- DB에 저장된 로컬 세션만 수집합니다. 저장 전 충돌, 이미 삭제된 과거 기록, 다른 PC의 DB는 복구하지 않습니다.\n- 원본 DB는 읽기 전용이며 삭제·prune하지 않습니다.\n- 시작하지 않은 새 Profile도 발견하면 안내 스킬·플러그인을 설치합니다. 실제 적용은 해당 Profile의 다음 새 세션에서 확인하세요.\n- 수집기는 실행할 때만 상태를 갱신합니다. 이 페이지의 마지막 수집 시간이 오래되면 작업 스케줄러를 확인하세요.\n- 치명적 오류 상세 분류는 로컬 runtime/fatal.json과 작업 스케줄러 LastTaskResult를 확인하세요."
        self.write("60_Operations/00_Archive-Health.md", note("archive-health", "수집 상태", health_body, updated=result["updated"], sessions=result["sessions"], errors=len(result["errors"]), generated=True))


def source_stamp(path):
    result = []
    for p in (Path(path), Path(str(path) + "-wal")):
        stat = p.stat() if p.exists() else None
        result.append([str(p), stat.st_size if stat else None, stat.st_mtime_ns if stat else None])
    return result


def bootstrap_profiles(archive, homes):
    boot = archive.state.setdefault("bootstrap", {})
    for profile, home in homes:
        try:
            bootstrap_profile(archive, profile, home)
        except Exception as exc:
            boot[profile] = {**boot.get(profile, {}), "status": "error"}
            archive.errors.append(archive.clean(f"Profile {profile} bootstrap: {type(exc).__name__}: {exc}"))
    return boot


def bootstrap_profile(archive, profile, home):
    home = Path(home)
    assert_managed_paths(Path(archive.cfg["base_home"]), [home])
    assert_managed_paths(home, [home / "config.yaml", home / "plugins", home / "skills"])
    assert_managed_paths(archive.runtime, [archive.state_path, archive.journal,
                         archive.state_path.with_name("state.json.archive-tmp")])
    bundle = Path(archive.cfg["source"]) / "archive-profile"
    boot = archive.state.setdefault("bootstrap", {})
    if not (home / "config.yaml").exists():
        boot[profile] = {"status": "pending-config"}
        return
    previous = boot.get(profile, {})
    hashes = previous.get("files", {})
    updated = False
    plugin = home / "plugins/hermes-archive-context"
    assignments = [(bundle / "SKILL.md", home / "skills/note-taking/hermes-archive-records/SKILL.md"),
                   (bundle / "SKILL.md", plugin / "SKILL.md"),
                   (bundle / "plugin.yaml", plugin / "plugin.yaml"),
                   (bundle / "__init__.py", plugin / "__init__.py")]
    payloads = [(origin.read_bytes(), target) for origin, target in assignments]
    settings = {key: str(archive.cfg[key]) for key in
                ("base_home", "archive_root", "vault_path", "archive_folder", "runtime", "source")}
    payloads.append((packed(settings).encode("utf-8"), plugin / "archive-settings.json"))
    assert_managed_paths(home, [path for _, target in payloads for path in
                              (target, target.with_name(target.name + ".archive-tmp"))])
    new_hashes = {}
    for data, target in payloads:
        wanted = digest(data)
        new_hashes[str(target)] = wanted
        current = digest(target.read_bytes()) if target.exists() else None
        if current is not None and current != wanted and current != hashes.get(str(target)):
            raise RuntimeError(f"Profile {profile}: archive-managed file was modified; preserved: {target}")
    boot[profile] = {**previous, "files": dict(hashes)}
    for data, target in payloads:
        wanted = digest(data)
        current = digest(target.read_bytes()) if target.exists() else None
        if current != wanted:
            archive.atomic_write(target, data)
            updated = True
        boot[profile]["files"][str(target)] = wanted
    config_stat = (home / "config.yaml").stat().st_mtime_ns
    if not updated and previous.get("config_stamp") == config_stat:
        return
    command = archive.cfg.get("hermes_command") or shutil.which("hermes")
    if not command:
        raise RuntimeError("Hermes CLI missing for Profile provisioning")
    env = {**os.environ, "HERMES_HOME": str(home.resolve()), "PYTHONIOENCODING": "utf-8"}
    def cli(*args):
        process = subprocess.run([command, *args], env=env, cwd=archive.cfg["source"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if process.returncode:
            missing = (process.stdout + process.stderr).strip().splitlines()
            if (args[:2] == ("config", "get") and process.returncode == 1 and missing
                    and missing[-1] == f"Config key not set: {args[2]}"):
                if args[2] == "plugins.disabled":
                    return "[]"
                if args[2] == "sessions.auto_prune":
                    return "unset"
            raise RuntimeError(archive.clean(f"Profile {profile} CLI {' '.join(args)} failed (exit {process.returncode}): {process.stderr[-1200:]} {process.stdout[-1200:]}"))
        return process.stdout
    disabled = cli("config", "get", "plugins.disabled")
    explicitly_disabled = "hermes-archive-context" in disabled
    if not explicitly_disabled:
        cli("plugins", "enable", "hermes-archive-context", "--no-allow-tool-override")
    observed = json.loads(cli("plugins", "list", "--json"))
    status = next((item["status"] for item in observed if item["name"] == "hermes-archive-context"), "missing")
    if not explicitly_disabled and status != "enabled":
        raise RuntimeError(f"Profile {profile}: archive context did not enable: {status}")
    # Preserve source history while the archive is being established. No source deletion ever.
    previous_prune = previous.get("previous_auto_prune")
    if "verified" not in previous:
        previous_prune = cli("config", "get", "sessions.auto_prune").strip().lower()
        cli("config", "set", "sessions.auto_prune", "false")
        if cli("config", "get", "sessions.auto_prune").strip().lower() != "false":
            raise RuntimeError(f"Profile {profile}: source-retention readback failed")
    boot[profile] = {"status": "disabled-by-user" if explicitly_disabled else status, "files": new_hashes,
                     "config_stamp": (home / "config.yaml").stat().st_mtime_ns, "verified": stamp(time.time()),
                     "previous_auto_prune": previous_prune}
    archive.save_state()  # A later Profile timeout must not undo earlier verified provisioning.


def run(cfg, force=False):
    archive = Archive(cfg)
    started = time.perf_counter()
    sessions_written = 0
    base = Path(cfg["base_home"])
    homes = [("default", base)]
    if (base / "profiles").is_dir():
        for p in sorted((base / "profiles").iterdir()):
            if not p.is_dir():
                continue
            try:
                assert_managed_paths(base, [p])
            except ValueError as exc:
                archive.errors.append(archive.clean(f"Profile {p.name}: {exc}"))
                continue
            homes.append((p.name, p))
    if cfg.get("bootstrap_profiles"):
        try:
            bootstrap_profiles(archive, homes)
        except Exception as exc:
            archive.errors.append(archive.clean(f"Profile bootstrap: {type(exc).__name__}: {exc}"))
    force = force or archive.state.get("collector_digest") != archive.collector_digest or time.time() - archive.state.get("last_full", 0) > 86400
    source_states = archive.state.setdefault("sources", {})
    for profile, home in homes:
        source = home / "state.db"
        if not source.exists():
            continue
        try:
            fingerprint = source_stamp(source)
            prior = source_states.get(str(source))
            missing = any(not (archive.root / p).exists() for s in archive.state["sessions"].values() if s["profile"] == profile for p in s["files"])
            if not force and prior == fingerprint and not missing:
                continue
            source_errors = len(archive.errors)
            with closing(db_read(source)) as db:
                if not {"sessions", "messages"}.issubset(tables(db)):
                    raise RuntimeError("Unsupported state.db schema")
                # ponytail: O(session count) metadata; indexed message reads only for recent/changed sessions.
                for s in rows(db, "sessions", SESSION_FIELDS + USAGE_FIELDS, order="ORDER BY started_at,id"):
                    try:
                        existing = archive.state["sessions"].get(f"{profile}:{s['id']}", {})
                        signature = digest(packed(s))
                        old = time.time() - float(s.get("last_activity_at") or s.get("started_at") or 0) > 86400
                        parent = archive.parent_record(profile, s.get("parent_session_id"))
                        # A subagent follows its parent's folder when the parent is renamed.
                        placed = not parent or existing.get("path", "").startswith(parent["path"].rsplit("/", 1)[0] + "/Subagents/")
                        if not force and not missing and placed and existing.get("signature") == signature and (s.get("ended_at") or old):
                            continue
                        messages = rows(db, "messages", MESSAGE_FIELDS, "WHERE session_id=?", (s["id"],), "ORDER BY timestamp,id")
                        sessions_written += int(archive.session(profile, s, messages))
                        archive.state["sessions"][f"{profile}:{s['id']}"]["signature"] = signature
                    except Exception as exc:
                        archive.errors.append(archive.clean(f"{profile}/{s['id']}: {type(exc).__name__}: {exc}"))
            if len(archive.errors) == source_errors:
                source_states[str(source)] = fingerprint
        except Exception as exc:
            archive.errors.append(archive.clean(f"{profile}: {type(exc).__name__}: {exc}"))
    for action in (archive.groups, archive.projects):
        try:
            action(base)
        except Exception as exc:
            archive.errors.append(archive.clean(f"{action.__name__}: {type(exc).__name__}: {exc}"))
    sessions_dir = archive.root / "40_Sessions"
    if sessions_dir.is_dir():  # Retry folders a watcher kept from an earlier move.
        for folder in sorted((p for p in sessions_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            archive.prune_empty(folder)
    result = {"sessions": len(archive.state["sessions"]), "sessions_written": sessions_written,
              "projects": len(archive.state.get("projects", {})), "groups": len(archive.state.get("groups", {})),
              "failures": sum(len(s["failures"]) for s in archive.state["sessions"].values()),
              "errors": archive.errors, "files_written": archive.written,
              "duration_seconds": round(time.perf_counter()-started, 3), "updated": stamp(time.time())}
    archive.indexes(homes, result)
    result["files_written"] = archive.written
    result["duration_seconds"] = round(time.perf_counter() - started, 3)
    if force and not archive.errors:
        archive.state["last_full"] = time.time()
        archive.state["collector_digest"] = archive.collector_digest
    archive.state["last_run"] = result
    archive.save_state()
    if not archive.errors:
        (archive.runtime / "fatal.json").unlink(missing_ok=True)
    return result


@contextmanager
def lock(runtime):
    path = Path(runtime) / "collector.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("archive-config.json"))
    parser.add_argument("--force", action="store_true", help="Full reconciliation")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    try:
        cfg = normalize_config(cfg)
    except Exception as exc:
        print(packed({"fatal": type(exc).__name__, "updated": stamp(time.time()), "config": str(args.config)}))
        return 2
    try:
        with lock(cfg["runtime"]):
            result = run(cfg, args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["errors"] else 0
    except Exception as exc:
        # Do not log raw DB content or credentials when the mandatory redactor is unavailable.
        report = {"fatal": type(exc).__name__, "updated": stamp(time.time()), "config": str(args.config)}
        target = Path(cfg["runtime"]) / "fatal.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(packed(report), encoding="utf-8")
        print(packed(report))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
