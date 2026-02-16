"""Context buffer accumulation and dispatch triggering.

The buffer accumulates changed items from watchers, tracks budget in
real-time, and signals when a dispatch should occur (buffer full or
drift threshold exceeded).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from engram.config import resolve_doc_paths
from engram.fold.chunker import compute_budget, scan_drift

log = logging.getLogger(__name__)


class ContextBuffer:
    """Manages the context accumulation buffer.

    Items flow in from watchers via :meth:`add_item` and the buffer
    signals readiness for dispatch via :meth:`should_dispatch`.

    Parameters
    ----------
    config:
        Engram config dict.
    project_root:
        Project root directory.
    db:
        ServerDB instance for persistent buffer state.
    """

    def __init__(
        self,
        config: dict[str, Any],
        project_root: Path,
        db: Any,
    ) -> None:
        self._config = config
        self._project_root = project_root
        self._db = db

    def add_item(
        self,
        path: str,
        item_type: str,
        chars: int = 0,
        date: str | None = None,
        metadata: str | None = None,
    ) -> bool:
        """Add an item to the buffer if not already present.

        Returns True if item was added, False if duplicate.
        """
        if self._db.has_buffer_item(path):
            log.debug("Skipping duplicate buffer item: %s", path)
            return False

        self._db.add_buffer_item(
            path=path,
            item_type=item_type,
            chars=chars,
            date=date,
            metadata=metadata,
        )
        log.info("Buffer += %s (%s, %d chars)", path, item_type, chars)
        return True

    def should_dispatch(self) -> str | None:
        """Check whether the buffer should trigger a dispatch.

        Returns a reason string if dispatch is warranted, None otherwise.
        Reasons: ``"buffer_full"``, ``"drift:<type>"``.
        """
        # Check drift thresholds
        drift = scan_drift(self._config, self._project_root)
        thresholds = self._config.get("thresholds", {})
        drift_type = drift.triggered(thresholds)
        if drift_type:
            return f"drift:{drift_type}"

        # Check buffer fill level against budget
        doc_paths = resolve_doc_paths(self._config, self._project_root)
        budget, _living_chars = compute_budget(self._config, doc_paths)
        buffer_chars = self._db.get_buffer_chars()

        if budget > 0 and buffer_chars >= budget:
            return "buffer_full"

        return None

    def get_items(self) -> list[dict[str, Any]]:
        """Return all buffer items."""
        return self._db.get_buffer_items()

    def get_fill_info(self) -> dict[str, Any]:
        """Return buffer fill information for status display."""
        doc_paths = resolve_doc_paths(self._config, self._project_root)
        budget, living_chars = compute_budget(self._config, doc_paths)
        buffer_chars = self._db.get_buffer_chars()
        items = self._db.get_buffer_items()

        fill_pct = (buffer_chars / budget * 100) if budget > 0 else 0.0

        return {
            "item_count": len(items),
            "buffer_chars": buffer_chars,
            "budget": budget,
            "living_docs_chars": living_chars,
            "fill_pct": min(fill_pct, 100.0),
        }

    def consume_all(self) -> list[dict[str, Any]]:
        """Consume all buffer items for dispatch. Returns consumed items."""
        items = self._db.get_buffer_items()
        if not items:
            return []
        item_ids = [item["id"] for item in items]
        return self._db.consume_buffer(item_ids)
