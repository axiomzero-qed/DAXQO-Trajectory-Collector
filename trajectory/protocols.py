"""Optional bridge contract; Dataset and Recorder do not require this backend."""
from __future__ import annotations

from typing import Protocol


class RecordingClient(Protocol):
    serial: str

    def request(self, path: str, body: dict | None = None) -> dict: ...
