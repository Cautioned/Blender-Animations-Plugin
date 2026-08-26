"""Small completion-driven registry for background import assets."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from queue import Empty, SimpleQueue
from threading import Lock
from typing import Any, Hashable, Iterable


class AssetState(Enum):
    pending = auto()
    fetching = auto()
    ready = auto()
    failed = auto()


@dataclass
class AssetRecord:
    key: str
    source: str
    kind: str
    state: AssetState = AssetState.pending
    payload: Any = None
    error: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in (AssetState.ready, AssetState.failed)


class AssetRegistry:
    """Own asset state and publish each terminal transition exactly once."""

    def __init__(self):
        self._records: dict[str, AssetRecord] = {}
        self._lock = Lock()
        self._completions = SimpleQueue()
        self._ready_consumers = SimpleQueue()
        self._consumer_pending: dict[Hashable, set[str]] = {}
        self._key_consumers: dict[str, set[Hashable]] = {}

    def register(self, key: str, source: str, kind: str) -> AssetRecord:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                record = AssetRecord(key=key, source=source, kind=kind)
                self._records[key] = record
            return record

    def mark_fetching(self, key: str) -> None:
        with self._lock:
            record = self._records[key]
            if record.state is AssetState.pending:
                record.state = AssetState.fetching

    def finish(self, key: str, payload=None, error: str = "", timings=None) -> None:
        with self._lock:
            record = self._records[key]
            if record.terminal:
                return
            record.payload = payload
            record.error = str(error or "")
            record.timings = dict(timings or {})
            record.state = AssetState.ready if not error else AssetState.failed
            self._completions.put(record)
            for consumer in self._key_consumers.pop(key, ()):
                pending = self._consumer_pending.get(consumer)
                if pending is None:
                    continue
                pending.discard(key)
                if not pending:
                    self._consumer_pending.pop(consumer, None)
                    self._ready_consumers.put(consumer)

    def subscribe(self, consumer: Hashable, keys: Iterable[str]) -> bool:
        """Publish consumer once all keys are terminal; return true if ready."""
        with self._lock:
            pending = {
                key for key in keys
                if key in self._records and not self._records[key].terminal
            }
            if not pending:
                return True
            self._consumer_pending[consumer] = pending
            for key in pending:
                self._key_consumers.setdefault(key, set()).add(consumer)
            return False

    def drain_completions(self) -> list[AssetRecord]:
        return self._drain(self._completions)

    def drain_ready_consumers(self) -> list[Hashable]:
        return self._drain(self._ready_consumers)

    @staticmethod
    def _drain(queue) -> list:
        values = []
        while True:
            try:
                values.append(queue.get_nowait())
            except Empty:
                return values

    def get(self, key: str):
        with self._lock:
            return self._records.get(key)

    def counts(self) -> dict[AssetState, int]:
        with self._lock:
            return {
                state: sum(record.state is state for record in self._records.values())
                for state in AssetState
            }

    def clear(self) -> None:
        """Release payloads, dependency edges, and queued record references."""
        with self._lock:
            self._records.clear()
            self._consumer_pending.clear()
            self._key_consumers.clear()
            self._drain(self._completions)
            self._drain(self._ready_consumers)
