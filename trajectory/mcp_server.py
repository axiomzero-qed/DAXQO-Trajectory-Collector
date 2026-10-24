"""Local stdio MCP recording endpoint for harnesses outside DAXQO."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from .dataset import Dataset
from .recorder import Recorder, snapshot

MAX_LINE = 4 * 1024 * 1024
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
RECORD_PROPERTIES = {
    "task_id": {"type": "string"}, "action_id": {"type": "string"},
    "action": {"type": "object"}, "before": {"type": "object"}, "after": {"type": "object"},
    "started_at": {"type": "string"}, "finished_at": {"type": "string"},
    "status": {"type": "string", "enum": ["succeeded", "failed", "unknown"]},
    "user_task": {"type": "string"}, "subgoal": {"type": "string"}, "stage": {"type": "string"},
    "model": {"type": ["string", "null"]}, "tool_call_id": {"type": ["string", "null"]},
    "candidate_actions": {"type": "array"}, "confidence": {"type": ["number", "null"]},
    "error_code": {"type": ["string", "null"]},
}
REQUIRED = ["task_id", "action_id", "action", "before", "after", "started_at", "finished_at"]
TOOLS = [
    {"name": "trajectory_record", "description": "Record one externally executed action; never performs device actions.",
     "inputSchema": {"type": "object", "properties": RECORD_PROPERTIES, "required": REQUIRED, "additionalProperties": False}},
    {"name": "trajectory_stats", "description": "Read local trajectory counts and quality statistics.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "trajectory_export", "description": "Export a dataset under the operator-configured export directory.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"},
         "split_by": {"type": "string", "enum": ["task", "device", "app"]}}, "additionalProperties": False}},
]


class CollectorMCP:
    def __init__(self, recorder: Recorder, export_dir: Path):
        self.recorder, self.export_dir = recorder, Path(export_dir)

    def call_tool(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        if name == "trajectory_record":
            if set(args) - RECORD_PROPERTIES.keys() or any(key not in args for key in REQUIRED):
                raise ValueError("Missing or unknown recording fields")
            # A source host reports its own verification, never human review.
            for key in ("task_id", "action_id", "started_at", "finished_at"):
                if not isinstance(args[key], str) or not args[key]:
                    raise ValueError("Required recording identifiers and timestamps must be non-empty strings")
            args = dict(args)
            for side in ("before", "after"):
                value = args[side]
                if not isinstance(value, dict) or set(value) - {"package_name", "nodes", "width", "height"}:
                    raise ValueError("States accept package_name, nodes, width and height")
                args[side] = snapshot(**value)
            return self.recorder.record_action(**args)
        if name == "trajectory_stats" and not args:
            return self.recorder.dataset.stats()
        if name == "trajectory_export" and not set(args) - {"name", "split_by"}:
            label = args.get("name", "scoring")
            if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", label):
                raise ValueError("Export name must be one simple directory name")
            split_by = args.get("split_by", "task")
            if split_by not in {"task", "device", "app"}:
                raise ValueError("Invalid split_by")
            destination = self.export_dir / label
            if destination.resolve().parent != self.export_dir.resolve() or destination.is_symlink():
                raise ValueError("Export must remain inside the configured directory")
            return self.recorder.dataset.export(destination, split_by=split_by)
        raise ValueError("Unknown tool or invalid arguments")

    def dispatch(self, message):
        ident = message.get("id") if isinstance(message, dict) else None
        def error(code, detail):
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": detail}}
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)):
            return error(-32600, "Invalid JSON-RPC request")
        if "id" not in message:
            return None
        if type(ident) not in (str, int):
            return error(-32600, "Invalid request id")
        method, params = message["method"], message.get("params", {})
        if not isinstance(params, dict):
            return error(-32602, "params must be an object")
        if method == "initialize":
            version = params.get("protocolVersion", VERSIONS[-1])
            if version not in VERSIONS:
                version = VERSIONS[-1]
            result = {"protocolVersion": version, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "daxqo-trajectory-collector", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                value = self.call_tool(params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}]}
            except Exception:
                # Exception values may echo UI inputs. Keep stderr/stdout clean.
                result = {"isError": True, "content": [{"type": "text", "text": "Recording request rejected; check schema, IDs and local dataset configuration."}]}
        else:
            return error(-32601, "Method not found")
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def serve(self, source=None, sink=None):
        source, sink = source or sys.stdin.buffer, sink or sys.stdout.buffer
        while line := source.readline(MAX_LINE + 1):
            try:
                if len(line) > MAX_LINE:
                    while line and not line.endswith(b"\n"):
                        line = source.readline(MAX_LINE + 1)
                    raise ValueError("oversize")
                response = self.dispatch(json.loads(line))
            except (ValueError, UnicodeError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON request"}}
            if response is not None:
                sink.write((json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
                sink.flush()
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--device-id", required=True, help="Stable UUID assigned by the operator")
    parser.add_argument("--session-id", help="Stable session UUID; set it to retry across restarts")
    parser.add_argument("--source", default="external_harness")
    parser.add_argument("--export-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        with Dataset(args.data) as dataset:
            recorder = Recorder(dataset, device_id=args.device_id, source=args.source, session_id=args.session_id)
            return CollectorMCP(recorder, args.export_dir or args.data / "exports").serve()
    except Exception as exc:
        print("Collector MCP startup failed: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
