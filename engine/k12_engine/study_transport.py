"""Administrator-owned study bindings and the fixed CLI transport."""
from __future__ import annotations

from contextvars import ContextVar

request_credentials = ContextVar("study_request_credentials", default=None)

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import urlsplit


class TransportError(ValueError):
    def __init__(self, state: str, reason: str, *, code=None):
        self.state = state
        self.reason = reason
        self.code = code
        super().__init__(reason)


def _workspace(cwd: str) -> str:
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        raise TransportError("blocked", "workspace must be an existing absolute directory")
    return os.path.normcase(str(path.resolve()))


def _store() -> Path:
    return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") / "profiles" / "k12-teacher" / "study-bindings.sqlite"


def load_binding(cwd: str) -> dict | None:
    workspace = _workspace(cwd)
    path = _store()
    if not path.is_file():
        return None
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT config FROM bindings WHERE workspace=?", (workspace,)).fetchone()
    return json.loads(row[0]) if row else None


def known_bindings() -> list[dict]:
    path = _store()
    if not path.is_file():
        return []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT config FROM bindings')]


def register_notification(cwd, session, receipt):
    """Trusted host notifications never acquire authority from a workspace ledger."""
    workspace = _workspace(cwd)
    path = _store()
    if not path.is_file() or path.resolve().is_relative_to(Path(workspace)):
        raise TransportError('blocked', 'trusted notification store unavailable')
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS practice_notifications(
            request_id TEXT PRIMARY KEY,workspace TEXT NOT NULL,session TEXT NOT NULL,
            task_id TEXT NOT NULL,submission_id TEXT NOT NULL)''')
        values = (receipt['requestId'], workspace, session, receipt['taskId'], receipt['submissionId'])
        previous = db.execute('SELECT * FROM practice_notifications WHERE request_id=?', (values[0],)).fetchone()
        if previous and previous != values:
            raise TransportError('blocked', 'notification association mismatch')
        db.execute('INSERT OR IGNORE INTO practice_notifications VALUES (?,?,?,?,?)', values)


def notification_ids(cwd, session):
    path = _store()
    if not path.is_file():
        return []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='practice_notifications'").fetchone():
            return []
        return [row[0] for row in db.execute('SELECT request_id FROM practice_notifications WHERE workspace=? AND session=?',
                                            (_workspace(cwd), session))]


def _config(cwd: str, config: dict) -> dict:
    if not isinstance(config, dict) or set(config) != {"studentId", "endpoint", "credentialRef", "command"}:
        raise TransportError("blocked", "invalid binding configuration fields")
    student = config["studentId"]
    credential = config["credentialRef"]
    if not isinstance(student, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", student):
        raise TransportError("blocked", "invalid expected student")
    if not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", credential):
        raise TransportError("blocked", "credentialRef must name an environment variable")
    try:
        parsed = urlsplit(config["endpoint"])
        valid_scheme = parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})
        valid = valid_scheme and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in {"", "/"}
        endpoint = f"{parsed.scheme}://{parsed.netloc}".lower()
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise TransportError("blocked", "endpoint must be an HTTPS origin or local test origin")
    command = config["command"]
    if not isinstance(command, list) or len(command) not in {1, 2} or not all(isinstance(p, str) and Path(p).is_absolute() and Path(p).is_file() for p in command):
        raise TransportError("blocked", "command must contain absolute executable paths")
    executable = Path(command[0]).stem.lower()
    if executable in {"cmd", "powershell", "pwsh", "sh", "bash", "python", "python3"} or Path(command[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise TransportError("blocked", "shell commands are not supported")
    if len(command) == 2 and (executable != "node" or Path(command[1]).suffix.lower() not in {".js", ".mjs", ".cjs"}):
        raise TransportError("blocked", "two-part command must be node and the CLI script")
    paths = [str(Path(p).resolve()) for p in command]
    artifacts = paths[:]
    if len(paths) == 2 and Path(paths[1]).name == "k12.js":
        bundle = Path(paths[1]).parent.parent / "dist" / "k12.mjs"
        if not bundle.is_file():
            raise TransportError("blocked", "CLI bundled artifact is missing")
        artifacts.append(str(bundle.resolve()))
    return {"workspace": _workspace(cwd), "studentId": student, "credentialRef": credential,
            "endpoint": endpoint, "endpointId": hashlib.sha256(endpoint.encode()).hexdigest(),
            "command": paths, "artifacts": artifacts, "commandHashes": [_hash(p) for p in artifacts],
            "learningRecordVersion": "learning-v1"}


def _hash(path: str) -> str:
    with open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def bind(cwd: str, config: dict, *, ui_authorized=False, display_name=None) -> dict:
    binding = _config(cwd, config)
    path = _store()
    if path.resolve().is_relative_to(Path(binding["workspace"])):
        raise TransportError("blocked", "binding store must be outside the student workspace")
    transport = Transport(binding)
    transport.verify()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute("CREATE TABLE IF NOT EXISTS bindings (workspace TEXT PRIMARY KEY, config TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT config FROM bindings WHERE workspace=?", (binding["workspace"],)).fetchone()
        old = json.loads(row[0]) if row else None
        if old and (old["studentId"], old["endpointId"]) != (binding["studentId"], binding["endpointId"]):
            raise TransportError("blocked", "use a new workspace for another student or endpoint")
        binding["bindingVersion"] = old["bindingVersion"] + 1 if old else 1
        if ui_authorized:
            binding["uiAuthorized"] = True
            binding["displayName"] = display_name or binding["studentId"]
        elif old and old.get("uiAuthorized"):
            binding["uiAuthorized"] = True
            binding["displayName"] = old.get("displayName", binding["studentId"])
        db.execute("INSERT OR REPLACE INTO bindings VALUES (?,?)", (binding["workspace"], json.dumps(binding)))
    if os.name != "nt":
        path.chmod(0o600)
    return binding


class Transport:
    def __init__(self, binding: dict):
        self.binding = binding
        self.verified = False
        self.env = {key: value for key, value in os.environ.items()
                    if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "HOME", "USERPROFILE", "LANG"}}
        private = request_credentials.get()
        if private and (private.get("credentialRef") != binding["credentialRef"]
                        or (private.get("bindingVersion") is not None and private["bindingVersion"] != binding.get("bindingVersion"))):
            raise TransportError("blocked", "binding changed before request")
        token = (private.get("token") if private and private.get("credentialRef") == binding["credentialRef"]
                 else os.environ.get(binding["credentialRef"]))
        if not token:
            raise TransportError("blocked", "credential reference is unavailable")
        self.env.update(K12_AGENT_TOKEN=token, K12_SITE_URL=binding["endpoint"])

    def _invoke(self, arguments: list[str], payload=None, history=False):
        try:
            if [_hash(p) for p in self.binding["artifacts"]] != self.binding["commandHashes"]:
                raise TransportError("blocked", "CLI installation changed; verify binding again")
            result = subprocess.run([*self.binding["command"], *arguments],
                                    input=None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                                    capture_output=True, text=True, encoding="utf-8", errors="strict",
                                    env=self.env, cwd=str(Path.home()), shell=False, timeout=70,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired):
            raise TransportError("pending", "study CLI unavailable or timed out") from None
        except UnicodeError:
            raise TransportError("blocked", "invalid CLI output encoding") from None
        try:
            envelope = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise TransportError("blocked", "invalid CLI response") from None
        if not isinstance(envelope, dict):
            raise TransportError("blocked", "invalid CLI response")
        if result.returncode == 0 and envelope.get("ok") is True and "data" in envelope:
            if history:
                if not isinstance(envelope["data"], list) or (envelope.get("nextCursor") is not None and not isinstance(envelope["nextCursor"], str)):
                    raise TransportError("blocked", "invalid CLI history response")
                return {"items": envelope["data"], "nextCursor": envelope.get("nextCursor")}
            return envelope["data"]
        error = envelope.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code == "KNOWLEDGE_VERSION_MISMATCH":
            raise TransportError("conflict", "knowledge catalog version changed; read manifest and retry", code=code)
        if code == "NOT_FOUND":
            raise TransportError("blocked", "requested Study resource does not exist", code=code)
        if code in {"CONFLICT", "TUTOR_CONFLICT", "REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT"}:
            raise TransportError("conflict", "remote revision or identity conflict")
        if code in {"UNAUTHORIZED", "FORBIDDEN"}:
            raise TransportError("blocked", "credentials invalid", code=code)
        if result.returncode == 3 or code in {"NETWORK_ERROR", "INTERNAL_ERROR", "RATE_LIMITED", "SERVICE_UNAVAILABLE"}:
            raise TransportError("pending", "study service temporarily unavailable")
        raise TransportError("blocked", "study request rejected")

    def verify(self) -> None:
        capability = self._invoke(["capabilities"])
        if not isinstance(capability, dict) or capability.get("learningRecordVersion") != "learning-v1":
            raise TransportError("blocked", "CLI learning-v1 capability required")
        identity = self._invoke(["whoami"])
        if (not isinstance(identity, dict) or not isinstance(identity.get("student"), dict)
                or identity["student"].get("id") != self.binding["studentId"]
                or not isinstance(identity.get("capabilities"), dict)
                or identity["capabilities"].get("learningRecordVersion") != "learning-v1"):
            raise TransportError("blocked", "student identity or server capability mismatch")
        self.verified = True

    def call(self, operation: str, payload=None, record_id=None, cursor=None, limit=20, kind=None,
             knowledge_id=None, exercise_id=None, query=None, resource_id=None, version=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise TransportError("blocked", "limit must be between 1 and 100")
        if operation in {"update", "detail", "task_show", "task_open", "task_certify", "task_revoke_certification"} and (not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", record_id)):
            raise TransportError("blocked", "invalid record reference")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1000):
            raise TransportError("blocked", "invalid cursor")
        for value in (knowledge_id, exercise_id):
            if value is not None and (operation not in {"recent", "wrong"} or not isinstance(value, str)
                                      or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value)):
                raise TransportError("blocked", "invalid history filter")
        knowledge_operations = {"knowledge_manifest", "knowledge_search", "knowledge_node", "knowledge_graph"}
        if operation in knowledge_operations - {"knowledge_search"} and limit != 20:
            raise TransportError("blocked", "limit is only supported for knowledge search")
        if query is not None and (operation != "knowledge_search" or not isinstance(query, str)
                                  or not query.strip() or len(query) > 100):
            raise TransportError("blocked", "invalid knowledge query")
        if operation == "knowledge_search" and query is None:
            raise TransportError("blocked", "knowledge search requires a query")
        if resource_id is not None and (operation not in {"knowledge_node", "knowledge_graph"}
                                        or not isinstance(resource_id, str)
                                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", resource_id)):
            raise TransportError("blocked", "invalid knowledge reference")
        if operation in {"knowledge_node", "knowledge_graph"} and resource_id is None:
            raise TransportError("blocked", "knowledge operation requires a reference")
        if version is not None and (operation not in {"knowledge_search", "knowledge_node", "knowledge_graph"}
                                    or not isinstance(version, str)
                                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", version)):
            raise TransportError("blocked", "invalid knowledge version")
        if operation not in knowledge_operations and (query is not None or resource_id is not None or version is not None):
            raise TransportError("blocked", "knowledge filters are not supported for this operation")
        commands = {"task_issue": ["task", "issue", "--file", "-"],
                    "task_assessment": ["task", "assessment", "--file", "-"],
                    "task_open": ["task", "open", record_id],
                    "task_certify": ["task", "certify", record_id, "--file", "-"],
                    "task_revoke_certification": ["task", "revoke-certification", record_id, "--file", "-"],
                    "task_show": ["task", "show", record_id],
                    "task_list": ["task", "list"],
                    "create": ["attempt", "record", "--file", "-"],
                    "update": ["attempt", "update", record_id, "--file", "-"],
                    "detail": ["attempt", "show", record_id],
                    "changes": ["attempt", "changes", "--after", cursor or "0", "--limit", str(limit)],
                    "recent": ["attempts", "--view", "learning-v1", "--limit", str(limit)],
                    "wrong": ["attempts", "--view", "learning-v1", "--wrong", "--limit", str(limit)],
                    "knowledge_manifest": ["kg", "manifest"],
                    "knowledge_search": ["kg", "catalog-search", "--limit", str(limit)],
                    "knowledge_node": ["kg", "catalog-show", resource_id],
                    "knowledge_graph": ["kg", "graph", resource_id],
                    "mastery": ["mastery"], "gaps": ["gaps"]}
        if operation not in commands:
            raise TransportError("blocked", "unsupported study operation")
        if payload is not None and operation not in {"create", "update", "task_issue", "task_assessment", "task_certify", "task_revoke_certification"}:
            raise TransportError("blocked", "payload is not accepted for this operation")
        arguments = commands[operation]
        if cursor and operation in {"recent", "wrong"}:
            arguments += ["--cursor", cursor]
        if knowledge_id is not None:
            arguments += ["--knowledge", knowledge_id]
        if exercise_id is not None:
            arguments += ["--exercise", exercise_id]
        if version is not None:
            arguments += ["--version", version]
        if operation == "knowledge_search":
            arguments += ["--", query]
        if not self.verified:
            self.verify()
        try:
            return self._invoke(arguments, payload, history=operation in {"recent", "wrong"})
        except TransportError as exc:
            if operation in knowledge_operations and exc.code == "NOT_FOUND":
                raise TransportError("blocked", "shared knowledge graph is unavailable or the requested node does not exist; read manifest and verify the ID", code=exc.code) from None
            raise
