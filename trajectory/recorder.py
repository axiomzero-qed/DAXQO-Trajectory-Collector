"""Small public recording API for any harness; no Android/ADB dependency."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import uuid

from .dataset import Dataset, SCHEMA_VERSION
from .privacy import redact_record


def snapshot(package_name: str, nodes=None, width=0, height=0) -> dict:
    """Build a schema-compatible observation from an external backend."""
    nodes = copy.deepcopy(nodes or [])
    fingerprint = hashlib.sha256(json.dumps([package_name, nodes, width, height],
        ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {"package_name": package_name, "nodes": nodes, "width": width, "height": height,
            "fingerprint": fingerprint, "training_allowed": True, "redacted": True}


class Recorder:
    """Record externally executed actions through Dataset's existing privacy gates.

    This class never executes actions. Supply observed states and outcomes from
    the harness. Stable action IDs and timestamps make retries idempotent.
    Model-reported success is always attributed to ``agent_claim``.
    """

    def __init__(self, dataset: Dataset, *, device_id: str, source="external_harness", session_id=None):
        self.dataset, self.device_id, self.source = dataset, str(uuid.UUID(device_id)), source
        self.session_id = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())

    def record_action(self, *, task_id: str, action_id: str, action: dict, before: dict, after: dict,
                      status="unknown", user_task="", subgoal="", stage="running",
                      started_at=None, finished_at=None, model=None, tool_call_id=None,
                      candidate_actions=None, confidence=None, error_code=None) -> dict:
        stamp = datetime.now(timezone.utc).isoformat()
        record_id = str(uuid.uuid5(uuid.UUID(self.device_id), self.session_id + ":action:" + action_id))
        record = {"schema_version": SCHEMA_VERSION, "record_type": "action", "record_id": record_id,
            "device": {"id": self.device_id}, "session_id": self.session_id, "action_id": action_id,
            "task": {"id": task_id, "user_task": user_task, "subgoal": subgoal, "stage": stage, "source": self.source},
            "started_at": started_at or stamp, "finished_at": finished_at or stamp,
            "before": copy.deepcopy(before), "after": copy.deepcopy(after),
            "action": {"type": action.get("type", "unknown"), "target": action.get("target", {}),
                       "parameters": action.get("parameters", {})},
            "decision": {"source": self.source, "model": model, "tool_call_id": tool_call_id,
                         "candidate_actions": candidate_actions or [], "confidence": confidence},
            "result": {"status": status, "error_code": error_code,
                       "verification": {"status": "unverified", "source": "agent_claim"}},
            "navigation": {}, "events": [], "metrics": {},
            "privacy": {"redacted": True, "excluded": False, "reasons": []}}
        # Public SDK identifiers and digests describe the sanitized record.
        # Raw UI and action inputs are not returned or logged by this API.
        record = redact_record(record)
        receipt = self.dataset.ingest([record], device_id=self.device_id)
        return {**receipt, "record_id": record_id, "action_id": action_id}
