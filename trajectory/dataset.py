"""Local, append-only trajectory storage and leakage-aware JSONL export."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import uuid
import copy
import statistics
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import is_sensitive_app, redact_record

SCHEMA_VERSION = "daxqo.trajectory/0.1"
_RECORD_TYPES = {"action", "agent_feedback", "task_event"}
_RESULTS = {"succeeded", "failed", "unknown"}
_VERIFY_SOURCES = {"fixture_assertion", "human_review", "agent_claim"}


@contextmanager
def _exclusive_file_lock(path: Path):
    """Cross-process lock for JSONL repair/appends on Windows and POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _uuid(value: Any, field: str) -> None:
    _require(isinstance(value, str), f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError(f"{field} must be a UUID string") from None
    _require(str(parsed) == value.lower(), f"{field} must be a canonical UUID")


def _iso(value: Any, field: str) -> None:
    _require(isinstance(value, str), f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be an ISO timestamp") from None
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed),
             f"{field} must be UTC")


def _mapping(value: Any, field: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{field} must be an object")
    return value


def _string(value: Any, field: str, *, nullable: bool = False) -> None:
    _require((nullable and value is None) or isinstance(value, str), f"{field} must be a string")


def _state(value: Any, field: str) -> None:
    state = _mapping(value, field)
    for key in ("fingerprint", "package_name"):
        _string(state.get(key), f"{field}.{key}")
    _require(isinstance(state.get("nodes"), list), f"{field}.nodes must be a list")
    for key in ("width", "height"):
        number = state.get(key)
        _require(type(number) in (int, float) and math.isfinite(number) and number >= 0,
                 f"{field}.{key} must be a non-negative number")
    for key in ("training_allowed", "redacted"):
        _require(type(state.get(key)) is bool, f"{field}.{key} must be boolean")


def validate_record(record: Any) -> dict[str, Any]:
    """Strictly check required protocol fields and primitive types."""
    _require(isinstance(record, dict), "record must be an object")
    _require(record.get("schema_version") == SCHEMA_VERSION, "unsupported schema_version")
    _require(record.get("record_type") in _RECORD_TYPES, "invalid record_type")
    _uuid(record.get("record_id"), "record_id")
    device = _mapping(record.get("device"), "device")
    _uuid(device.get("id"), "device.id")
    for field in ("session_id",):
        _uuid(record.get(field), field)
    task = _mapping(record.get("task"), "task")
    for field in ("id", "user_task", "subgoal", "stage", "source"):
        _string(task.get(field), f"task.{field}")
    _string(record.get("action_id"), "action_id")
    _require(bool(record["action_id"]), "action_id must not be empty")
    _iso(record.get("started_at"), "started_at")
    _iso(record.get("finished_at"), "finished_at")

    if record["record_type"] == "action":
        _state(record.get("before"), "before")
        _state(record.get("after"), "after")
        action = _mapping(record.get("action"), "action")
        _string(action.get("type"), "action.type")
        _require(isinstance(action.get("target"), (str, dict)), "action.target must be a string or object")
        _mapping(action.get("parameters"), "action.parameters")
        decision = _mapping(record.get("decision"), "decision")
        for field in ("source", "model", "tool_call_id"):
            _string(decision.get(field), f"decision.{field}", nullable=True)
        _require(isinstance(decision.get("candidate_actions"), list), "decision.candidate_actions must be a list")
        confidence = decision.get("confidence")
        _require(confidence is None or (type(confidence) in (int, float) and math.isfinite(confidence)
                                        and 0 <= confidence <= 1), "decision.confidence must be null or in [0,1]")
        result = _mapping(record.get("result"), "result")
        _require(result.get("status") in _RESULTS, "invalid result.status")
        _string(result.get("error_code"), "result.error_code", nullable=True)
        verification = _mapping(result.get("verification"), "result.verification")
        _require(isinstance(verification.get("status"), str), "result.verification.status must be a string")
        _mapping(record.get("navigation"), "navigation")
        _require(isinstance(record.get("events"), list), "events must be a list")
        _mapping(record.get("metrics"), "metrics")
        privacy = _mapping(record.get("privacy"), "privacy")
        _require(type(privacy.get("redacted")) is bool, "privacy.redacted must be boolean")
        _require(type(privacy.get("excluded")) is bool, "privacy.excluded must be boolean")
        _require(isinstance(privacy.get("reasons"), list) and all(isinstance(x, str) for x in privacy["reasons"]),
                 "privacy.reasons must be a list of strings")
    elif record["record_type"] == "agent_feedback":
        _mapping(record.get("decision", {}), "decision")
        verification = _mapping(record.get("verification", {"status": "unverified"}), "verification")
        _require(verification.get("status", "unverified") in ("verified", "unverified"),
                 "feedback verification.status must be verified or unverified")
        if verification.get("status", "unverified") == "verified":
            _require(verification.get("source") in _VERIFY_SOURCES, "verified feedback requires a valid source")
    return record


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _normalized_task(value: Any) -> str:
    import re
    return " ".join(re.sub(r"\s+", " ", str(value or "")).strip().casefold().split())


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


_EPHEMERAL_ACTION_KEYS = {
    "snapshotid", "requestid", "actionid", "time", "timestamp", "startedat",
    "finishedat", "createdat", "updatedat", "requesttimestamp", "sequence",
}


def _stable_action_signature(action: Any) -> Any:
    """Drop per-call identifiers from dedup signatures while retaining audit data."""
    if not isinstance(action, dict):
        return action
    result = dict(action)
    parameters = result.get("parameters")

    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if "".join(char for char in str(key).casefold() if char.isalnum()) not in _EPHEMERAL_ACTION_KEYS}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    if isinstance(parameters, dict):
        result["parameters"] = stable(parameters)
    return result


def _merged_decision(action_decision: dict[str, Any], feedback: list[dict[str, Any]]) -> dict[str, Any]:
    merged = copy.deepcopy(action_decision)
    for row in feedback:
        decision = row.get("decision", {})
        if not isinstance(decision, dict):
            continue
        for key, value in decision.items():
            if value not in (None, [], {}):
                merged[key] = copy.deepcopy(value)
    return merged


def _task_score(candidate: dict[str, Any]) -> tuple[int, int]:
    populated = sum(bool(candidate.get(key)) for key in ("user_task", "subgoal", "stage", "source"))
    meaningful_source = int(candidate.get("source") not in (None, "", "unattributed"))
    meaningful_prompt = int(bool(candidate.get("user_task")) and candidate.get("user_task") != "[redacted]")
    return meaningful_source * 4 + meaningful_prompt * 3 + populated, len(str(candidate.get("user_task") or ""))


def _task_context(action_task: dict[str, Any], feedback: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [row.get("task") for row in feedback if isinstance(row.get("task"), dict)]
    if candidates:
        best = max(candidates, key=_task_score)
        if _task_score(best) > _task_score(action_task):
            return best
    return action_task


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


class Dataset:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite3"
        self.jsonl_path = self.root / "trajectories.jsonl"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS records(
              record_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, stream_id TEXT NOT NULL,
              sequence INTEGER, task_id TEXT NOT NULL, record_type TEXT NOT NULL,
              payload TEXT NOT NULL, ordinal INTEGER NOT NULL UNIQUE, raw_digest TEXT
            );
            CREATE INDEX IF NOT EXISTS records_task ON records(task_id, ordinal);
            CREATE INDEX IF NOT EXISTS records_device ON records(device_id, ordinal);
            CREATE TABLE IF NOT EXISTS checkpoints(
              device_id TEXT NOT NULL, stream_id TEXT NOT NULL, sequence INTEGER NOT NULL,
              PRIMARY KEY(device_id, stream_id)
            );
        """)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(records)")}
        if "raw_digest" not in columns:
            self._db.execute("ALTER TABLE records ADD COLUMN raw_digest TEXT")
        self._recover_jsonl()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "Dataset":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _recover_jsonl(self) -> None:
        """Repair a torn tail, then append DB-committed rows missing from JSONL."""
        with self._lock:
            self._sync_jsonl()

    def _sync_jsonl(self) -> None:
        with _exclusive_file_lock(self.root / ".trajectories.lock"):
            known: set[str] = set()
            if self.jsonl_path.exists():
                raw = self.jsonl_path.read_bytes()
                complete_end = raw.rfind(b"\n") + 1
                if complete_end != len(raw):
                    with self.jsonl_path.open("r+b") as stream:
                        stream.truncate(complete_end)
                        stream.flush()
                        os.fsync(stream.fileno())
                    raw = raw[:complete_end]
                for line_number, line in enumerate(raw.splitlines(), 1):
                    try:
                        row = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise ValueError(f"invalid JSONL row at line {line_number}") from None
                    if not isinstance(row, dict) or not isinstance(row.get("record_id"), str):
                        raise ValueError(f"invalid JSONL record at line {line_number}")
                    row_id = row["record_id"]
                    indexed = self._db.execute("SELECT payload FROM records WHERE record_id=?", (row_id,)).fetchone()
                    if indexed is None or indexed[0] != _json(row):
                        raise ValueError(f"JSONL/index mismatch for record {row_id}")
                    if row_id in known:
                        raise ValueError(f"duplicate JSONL record {row_id}")
                    known.add(row_id)
            rows = self._db.execute("SELECT payload FROM records ORDER BY ordinal").fetchall()
            missing = [row[0] for row in rows if json.loads(row[0])["record_id"] not in known]
            if missing:
                with self.jsonl_path.open("ab") as stream:
                    for payload in missing:
                        stream.write(payload.encode("utf-8") + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())

    def ingest(self, records: list[dict[str, Any]], device_id: str | None = None) -> dict[str, int]:
        _require(isinstance(records, list), "records must be a list")
        # Sanitize and validate the entire batch before any transaction or file write.
        prepared: list[tuple[dict[str, Any], str, str, str, int | None, str, str]] = []
        seen: dict[str, str] = {}
        for raw in records:
            _require(isinstance(raw, dict), "each record must be an object")
            raw_digest = hashlib.sha256(_json(raw).encode("utf-8")).hexdigest()
            item = redact_record(raw)
            if item.get("record_type") == "agent_feedback":
                item.setdefault("decision", {})
                item.setdefault("verification", {"status": "unverified"})
                _mapping(item["verification"], "verification")
                item["verification"].setdefault("status", "unverified")
            validate_record(item)
            actual_device = item["device"]["id"]
            if device_id is not None:
                _require(actual_device == device_id, "record device.id does not match device_id")
            stream = item.get("stream_id", "default")
            sequence = item.get("sequence")
            _require(isinstance(stream, str) and stream, "stream_id must be a non-empty string")
            _require(sequence is None or (type(sequence) is int and sequence >= 0), "sequence must be a non-negative integer")
            payload = _json(item)
            record_id = item["record_id"]
            if record_id in seen and seen[record_id] != raw_digest:
                raise ValueError(f"record_id {record_id} has conflicting batch content")
            seen[record_id] = raw_digest
            prepared.append((item, actual_device, stream, item["task"]["id"], sequence, payload, raw_digest))

        inserted_ids: list[str] = []
        duplicates = 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                ordinal = self._db.execute("SELECT COALESCE(MAX(ordinal), 0) FROM records").fetchone()[0]
                for item, actual_device, stream, task_id, sequence, payload, raw_digest in prepared:
                    record_id = item["record_id"]
                    existing = self._db.execute("SELECT payload,raw_digest FROM records WHERE record_id=?", (record_id,)).fetchone()
                    if existing:
                        if existing[0] != payload:
                            raise ValueError(f"record_id {record_id} has conflicting content")
                        if existing[1] is not None and existing[1] != raw_digest:
                            raise ValueError(f"record_id {record_id} has conflicting raw content")
                        if existing[1] is None:
                            self._db.execute("UPDATE records SET raw_digest=? WHERE record_id=?", (raw_digest, record_id))
                        duplicates += 1
                        continue
                    ordinal += 1
                    self._db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)",
                                     (record_id, actual_device, stream, sequence, task_id,
                                      item["record_type"], payload, ordinal, raw_digest))
                    inserted_ids.append(record_id)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            # A process-wide file lock plus DB-driven missing-row scan handles
            # concurrent Dataset instances and recovers commits after a crash.
            if inserted_ids:
                self._sync_jsonl()
        return {"inserted": len(inserted_ids), "duplicates": duplicates}

    def cursor(self, device_id: str, stream_id: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT sequence FROM checkpoints WHERE device_id=? AND stream_id=?",
                                   (device_id, stream_id)).fetchone()
            return int(row[0]) if row else 0

    def checkpoint(self, device_id: str, stream_id: str, sequence: int) -> None:
        _require(isinstance(device_id, str) and device_id, "device_id is required")
        _require(isinstance(stream_id, str) and stream_id, "stream_id is required")
        _require(type(sequence) is int and sequence >= 0, "sequence must be a non-negative integer")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self.cursor(device_id, stream_id)
                _require(sequence >= current, "checkpoint cannot move backwards")
                self._db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(device_id,stream_id) DO UPDATE SET sequence=excluded.sequence",
                                 (device_id, stream_id, sequence))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def records(self, task_id: str | None = None, device_id: str | None = None) -> list[dict[str, Any]]:
        clauses, args = [], []
        if task_id is not None:
            clauses.append("task_id=?"); args.append(task_id)
        if device_id is not None:
            clauses.append("device_id=?"); args.append(device_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._db.execute("SELECT payload FROM records" + where + " ORDER BY ordinal", args).fetchall()
        # Reapply the current guard to legacy records without rewriting audit data.
        return [redact_record(json.loads(row[0])) for row in rows]

    def stats(self) -> dict[str, Any]:
        rows = self.records()
        type_counts = Counter(row["record_type"] for row in rows)
        actions = [row for row in rows if row["record_type"] == "action"]
        feedback: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["record_type"] == "agent_feedback":
                feedback[(row["device"]["id"], row["action_id"])].append(row)

        outcomes: Counter[str] = Counter()
        verification: Counter[str] = Counter()
        excluded_privacy = 0
        by_device: dict[str, Counter[str]] = defaultdict(Counter)
        by_app: Counter[str] = Counter()
        transition_counts: Counter[str] = Counter()
        task_ids: set[str] = set()
        timing_values: dict[str, list[float]] = defaultdict(list)
        for row in actions:
            status = row["result"]["status"]
            outcomes[status] += 1
            device_id = row["device"]["id"]
            by_device[device_id]["actions"] += 1
            by_device[device_id][status] += 1
            app = row["after"]["package_name"]
            by_app["sensitive_app" if is_sensitive_app(app) else app] += 1
            blocked = (row["privacy"]["excluded"] or not row["before"]["training_allowed"]
                      or not row["after"]["training_allowed"]
                      or is_sensitive_app(row["before"]["package_name"])
                      or is_sensitive_app(row["after"]["package_name"]))
            if blocked:
                excluded_privacy += 1
                by_device[device_id]["privacy_excluded"] += 1

            related = feedback.get((device_id, row["action_id"]), [])
            task_ids.add(_task_context(row["task"], related)["id"])
            independently_verified = any(
                f.get("verification", {}).get("status") == "verified"
                and f.get("verification", {}).get("source") in ("fixture_assertion", "human_review")
                for f in related)
            has_agent_claim = any(f.get("verification", {}).get("source") == "agent_claim" for f in related)
            verification["independently_verified" if independently_verified else
                         "agent_claim_only" if has_agent_claim else "unverified"] += 1

            candidates = _merged_decision(row["decision"], related).get("candidate_actions")
            signature = _fingerprint([row["before"].get("fingerprint"), candidates,
                                      _stable_action_signature(row["action"]), row["after"].get("fingerprint")])
            transition_counts[signature] += 1
            for name in ("tree_ms", "observer_ms", "total_ms"):
                value = row["metrics"].get(name)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    timing_values[name].append(float(value))

        timings: dict[str, Any] = {}
        for name in ("tree_ms", "observer_ms", "total_ms"):
            values = sorted(timing_values[name])
            if values:
                p95_index = max(0, math.ceil(0.95 * len(values)) - 1)
                timings[name] = {"samples": len(values), "average": statistics.fmean(values),
                                 "p95": values[p95_index]}
            else:
                timings[name] = {"samples": 0, "average": None, "p95": None}
        return {
            "records": len(rows), "by_type": dict(type_counts), "devices": len(by_device),
            "tasks": len(task_ids), "actions": len(actions),
            "outcomes": {key: outcomes[key] for key in sorted(_RESULTS)},
            "verification": {key: verification[key] for key in
                             ("independently_verified", "agent_claim_only", "unverified")},
            "privacy_excluded": excluded_privacy,
            "duplicate_transitions": sum(value - 1 for value in transition_counts.values() if value > 1),
            "by_device": {device: dict(counts) for device, counts in by_device.items()},
            "by_app": dict(by_app), "timings_ms": timings,
        }

    def export(self, output: Path, split_by: str = "task", test_fraction: float = 0.2,
               seed: str = "daxqo-v0.1", include_unverified: bool = False) -> dict[str, Any]:
        _require(split_by in ("task", "device", "app"), "split_by must be task, device, or app")
        _require(type(test_fraction) in (int, float) and math.isfinite(test_fraction)
                 and 0 <= test_fraction < 1, "test_fraction must be in [0,1)")
        _require(isinstance(seed, str), "seed must be a string")
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        raw = self.records()
        actions = {(row["device"]["id"], row["action_id"]): row
                   for row in raw if row.get("record_type") == "action"}
        feedback: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in raw:
            if row.get("record_type") == "agent_feedback":
                feedback.setdefault((row["device"]["id"], row["action_id"]), []).append(row)

        eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
        excluded = {"privacy": 0, "sensitive_app": 0, "unverified_success": 0,
                    "unknown": 0, "transition_duplicate": 0}
        transition_owner: dict[str, int] = {}
        eligible_aliases: list[set[str]] = []
        for (device_id, action_id), row in actions.items():
            privacy = row["privacy"]
            if privacy["excluded"] or not row["before"]["training_allowed"] or not row["after"]["training_allowed"]:
                excluded["privacy"] += 1; continue
            if is_sensitive_app(row["before"]["package_name"]) or is_sensitive_app(row["after"]["package_name"]):
                excluded["sensitive_app"] += 1; continue
            related = feedback.get((device_id, action_id), [])
            merged_decision = _merged_decision(row["decision"], related)
            task = _task_context(row["task"], related)
            result = row["result"]["status"]
            independent = [f for f in related
                           if f.get("verification", {}).get("status") == "verified"
                           and f.get("verification", {}).get("source") in ("fixture_assertion", "human_review")]
            independent.sort(key=lambda f: 0 if f["verification"]["source"] == "human_review" else 1)
            verdict = independent[0] if independent else None
            if result == "failed":
                label, reward = "negative", 0
            elif result == "succeeded" and verdict:
                label, reward = "positive", 1
            elif result == "succeeded" and include_unverified:
                label, reward = "unverified", None
            elif result == "succeeded":
                excluded["unverified_success"] += 1; continue
            elif include_unverified:
                label, reward = "unverified", None
            else:
                excluded["unknown"] += 1; continue
            transition = _fingerprint([row["before"].get("fingerprint"), merged_decision.get("candidate_actions"),
                                       _stable_action_signature(row["action"]), row["after"].get("fingerprint")])
            aliases = {"task:" + task["id"], "template:" + _normalized_task(task["user_task"]),
                       "transition:" + transition}
            if split_by == "device":
                aliases.add("device:" + row["device"]["id"])
            elif split_by == "app":
                aliases.update(("app:" + row["before"]["package_name"].casefold(),
                                "app:" + row["after"]["package_name"].casefold()))
            if transition in transition_owner:
                excluded["transition_duplicate"] += 1
                # Keep the single exported sample, but preserve every duplicate's
                # task/device/app links so its neighboring transitions stay grouped.
                eligible_aliases[transition_owner[transition]].update(aliases)
                continue
            item = {"state": row["before"], "candidate_actions": merged_decision.get("candidate_actions", []),
                    "action": row["action"], "next_state": row["after"], "reward": reward,
                    "label": label, "confidence": merged_decision.get("confidence"),
                    "decision": merged_decision, "device_id": row["device"]["id"], "task_id": task["id"],
                    "task": task, "action_id": action_id}
            has_fixture_assertion = any(f.get("verification", {}).get("status") == "verified"
                                        and f.get("verification", {}).get("source") == "fixture_assertion"
                                        for f in related)
            if (has_fixture_assertion
                    or task.get("source") == "fixture" or row["task"].get("source") == "fixture"):
                item["data_origin"] = "fixture"
            transition_owner[transition] = len(eligible)
            eligible.append((row, item))
            eligible_aliases.append(aliases)

        # Union retained samples by aliases collected from all duplicate rows.
        groups = _UnionFind(len(eligible))
        keys: dict[str, int] = {}
        for i, group_keys in enumerate(eligible_aliases):
            for key in group_keys:
                if key in keys:
                    groups.union(i, keys[key])
                else:
                    keys[key] = i
        components: dict[int, list[int]] = {}
        for i in range(len(eligible)):
            components.setdefault(groups.find(i), []).append(i)
        component_ids = sorted(components, key=lambda root: hashlib.sha256(
            (seed + "\0" + min(eligible[i][1]["action_id"] for i in components[root])).encode()).hexdigest())
        test_roots: set[int] = set()
        if test_fraction > 0 and len(component_ids) > 1:
            for root in component_ids:
                digest = hashlib.sha256((seed + "\0" + min(eligible[i][1]["action_id"] for i in components[root])).encode()).digest()
                if int.from_bytes(digest[:8], "big") / 2**64 < test_fraction:
                    test_roots.add(root)
            if not test_roots:
                test_roots.add(component_ids[-1])
            if len(test_roots) == len(component_ids):
                test_roots.remove(component_ids[0])
        train: list[dict[str, Any]] = []
        test: list[dict[str, Any]] = []
        for i, (_row, item) in enumerate(eligible):
            (test if groups.find(i) in test_roots else train).append(item)

        self._atomic_jsonl(output / "train.jsonl", train)
        self._atomic_jsonl(output / "test.jsonl", test)
        limitations = []
        if len(component_ids) < 2:
            limitations.append("Only one leakage-safe group is available; test split is empty.")
        manifest = {"schema_version": SCHEMA_VERSION, "split_by": split_by,
                    "test_fraction": test_fraction, "seed": seed,
                    "counts": {"train": len(train), "test": len(test), "eligible": len(eligible)},
                    "excluded": excluded, "groups": len(component_ids), "limitations": limitations}
        manifest_path = output / "manifest.json"
        temp = manifest_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(manifest_path)
        return manifest

    @staticmethod
    def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                for row in rows:
                    stream.write(_json(row) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
