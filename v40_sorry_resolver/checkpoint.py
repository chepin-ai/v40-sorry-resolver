"""Atomic checkpointing for the v40 sorry resolver (M1).

Contract: SPEC.md section 3.5. ``save`` writes ``path.tmp`` and then
``os.replace`` (atomic; v39 bug P1-5). ``load`` never raises: any error
returns None. Tasks are serialized via ``SorryTask.to_dict``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .models import ResolutionResult, SorryTask

__all__ = ["Checkpoint"]

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1


class Checkpoint:
    """Save/load run state (tasks, results, phase, metrics)."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------
    def save(self, tasks, results, phase: str, metrics) -> None:
        """Atomically persist run state (tmp file + os.replace)."""
        payload = {
            "version": CHECKPOINT_VERSION,
            "phase": phase,
            "saved_at": time.time(),
            "tasks": [self._serialize_task(t) for t in (tasks or [])],
            "results": self._serialize_results(results),
            "metrics": metrics if metrics is not None else {},
        }
        parent = self.path.parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.path)

    # ------------------------------------------------------------------
    def load(self) -> Optional[dict]:
        """Load run state; return None on any error."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("checkpoint root is not a dict")
            tasks = [
                SorryTask.from_dict(d) for d in data.get("tasks", []) or []
            ]
            results = self._deserialize_results(data.get("results"))
            return {
                "version": data.get("version", 0),
                "phase": data.get("phase", ""),
                "saved_at": data.get("saved_at", 0.0),
                "tasks": tasks,
                "results": results,
                "metrics": data.get("metrics", {}) or {},
            }
        except Exception as exc:  # tolerant by contract
            logger.warning("checkpoint load failed (%s): %s", self.path, exc)
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _serialize_task(task):
        if hasattr(task, "to_dict"):
            return task.to_dict()
        return task

    @staticmethod
    def _serialize_results(results) -> dict:
        if isinstance(results, dict):
            return {
                "kind": "mapping",
                "items": {
                    str(k): (v.to_dict() if hasattr(v, "to_dict") else v)
                    for k, v in results.items()
                },
            }
        return {
            "kind": "list",
            "items": [
                (v.to_dict() if hasattr(v, "to_dict") else v)
                for v in (results or [])
            ],
        }

    @staticmethod
    def _deserialize_results(raw):
        def restore(item):
            if (
                isinstance(item, dict)
                and "task_id" in item
                and "status" in item
            ):
                return ResolutionResult.from_dict(item)
            return item

        if not isinstance(raw, dict) or "kind" not in raw:
            return []
        if raw.get("kind") == "mapping":
            return {k: restore(v) for k, v in (raw.get("items") or {}).items()}
        return [restore(v) for v in (raw.get("items") or [])]
