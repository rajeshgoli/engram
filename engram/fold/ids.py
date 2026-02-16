"""Stable ID allocation backed by SQLite.

Provides monotonic, never-reused IDs for concepts (C), epistemic claims (E),
and workflows (W). IDs are pre-assigned before fold agent dispatch so that
chunks processed in any order produce disjoint ID ranges.

The counter state lives in the ``id_counters`` table of ``.engram/engram.db``.
All reads and writes use SQL transactions for concurrent safety.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

# Valid ID categories and their prefixes
CATEGORIES = {"C": "C", "E": "E", "W": "W"}


class IDAllocatorError(Exception):
    """Raised on invalid allocation requests."""


class IDAllocator:
    """Monotonic ID counter backed by SQLite.

    Each category (C, E, W) has an independent counter that only moves
    forward. IDs are never reused, even after deletion.

    Supports context manager protocol::

        with IDAllocator(db_path) as alloc:
            alloc.next_id("C")

    Parameters
    ----------
    db_path:
        Path to the SQLite database file (typically ``.engram/engram.db``).
        Created if it does not exist.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        """Create a new connection with WAL mode enabled."""
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_table(self) -> None:
        """Create the id_counters table if it doesn't exist.

        Each row stores the *next available* ID for a category.
        Counters start at 1 (first assigned ID will be 1).
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_counters (
                    category TEXT PRIMARY KEY,
                    next_id  INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            for cat in CATEGORIES:
                conn.execute(
                    "INSERT OR IGNORE INTO id_counters (category, next_id) VALUES (?, 1)",
                    (cat,),
                )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> IDAllocator:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Core allocation
    # ------------------------------------------------------------------

    def next_id(self, category: str) -> str:
        """Allocate and return a single ID (e.g. ``"C001"``)."""
        ids = self.reserve_range(category, 1)
        return ids[0]

    def reserve_range(self, category: str, count: int) -> list[str]:
        """Atomically reserve *count* sequential IDs for *category*.

        Returns a list of formatted ID strings (e.g. ``["C042", "C043"]``).
        The counter advances by *count*; those IDs can never be issued again.

        Raises
        ------
        IDAllocatorError
            If *category* is invalid or *count* < 1.
        """
        _validate_category(category)
        if count < 1:
            raise IDAllocatorError(f"count must be >= 1, got {count}")

        prefix = CATEGORIES[category]

        # BEGIN IMMEDIATE acquires a write lock up front so concurrent
        # callers serialize at the SQLite level.
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            start = _reserve_on_conn(conn, category, count)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return [f"{prefix}{i:03d}" for i in range(start, start + count)]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def peek(self, category: str) -> int:
        """Return the next available ID number for *category* without advancing."""
        _validate_category(category)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT next_id FROM id_counters WHERE category = ?",
                (category,),
            ).fetchone()
            return row[0] if row else 1
        finally:
            conn.close()

    def peek_all(self) -> dict[str, int]:
        """Return ``{category: next_id}`` for all categories."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT category, next_id FROM id_counters").fetchall()
            return {cat: nid for cat, nid in rows}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Chunk pre-assignment
    # ------------------------------------------------------------------

    def pre_assign_for_chunk(
        self,
        new_concepts: int = 0,
        new_epistemic: int = 0,
        new_workflows: int = 0,
    ) -> dict[str, list[str]]:
        """Atomically reserve ID ranges across all categories for a chunk.

        All category counters are updated in a single transaction — a crash
        cannot leave counters partially advanced for a chunk.

        This is the main entry point used by the chunker before dispatch.

        Returns
        -------
        dict
            ``{"C": ["C042", ...], "E": ["E034", ...], "W": ["W015", ...]}``
            Only categories with count > 0 are included.
        """
        requests = [
            ("C", new_concepts),
            ("E", new_epistemic),
            ("W", new_workflows),
        ]
        # Skip if nothing to reserve
        if all(count <= 0 for _, count in requests):
            return {}

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ranges: dict[str, tuple[str, int, int]] = {}  # cat -> (prefix, start, count)
            for cat, count in requests:
                if count > 0:
                    start = _reserve_on_conn(conn, cat, count)
                    ranges[cat] = (CATEGORIES[cat], start, count)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {
            cat: [f"{prefix}{i:03d}" for i in range(start, start + count)]
            for cat, (prefix, start, count) in ranges.items()
        }

    def close(self) -> None:
        """No-op for API compatibility. All connections are per-call."""


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _validate_category(category: str) -> None:
    """Raise if category is not one of C, E, W."""
    if category not in CATEGORIES:
        raise IDAllocatorError(
            f"Invalid category '{category}'. Must be one of: {sorted(CATEGORIES)}"
        )


def _reserve_on_conn(conn: sqlite3.Connection, category: str, count: int) -> int:
    """Reserve *count* IDs for *category* using an existing connection.

    The caller must manage the transaction (BEGIN/COMMIT/ROLLBACK).
    Returns the starting ID number.
    """
    row = conn.execute(
        "SELECT next_id FROM id_counters WHERE category = ?",
        (category,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO id_counters (category, next_id) VALUES (?, ?)",
            (category, 1),
        )
        start = 1
    else:
        start = row[0]
    conn.execute(
        "UPDATE id_counters SET next_id = ? WHERE category = ?",
        (start + count, category),
    )
    return start


def estimate_new_entities(items: Sequence[dict]) -> dict[str, int]:
    """Estimate how many new entities a chunk's items will produce.

    Scans chunk items for ``entity_hints``. Items without hints
    are ignored (they update existing entries rather than creating new ones).

    Parameters
    ----------
    items:
        Sequence of dicts, each with at least a ``type`` key.
        Optional ``entity_hints`` key is a list of ``{"category": "C"|"E"|"W"}``.

    Returns
    -------
    dict
        ``{"C": n, "E": n, "W": n}`` — estimated new entity counts.
    """
    counts = {"C": 0, "E": 0, "W": 0}
    for item in items:
        for hint in item.get("entity_hints", []):
            cat = hint.get("category")
            if cat in counts:
                counts[cat] += 1
    return counts
