"""SQLite state management for the engram server.

Manages three tables in ``.engram/engram.db``:

- ``buffer_items``: accumulated context items pending dispatch
- ``dispatches``: dispatch lifecycle (building → dispatched → validated → committed)
- ``server_state``: singleton row with polling bookmarks

The ``id_counters`` table is owned by :mod:`engram.fold.ids` and is NOT
managed here. Both modules share the same database file.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Dispatch lifecycle states
DISPATCH_STATES = ("building", "dispatched", "validated", "committed")
TERMINAL_STATES = ("committed",)


class ServerDB:
    """SQLite state manager for the engram server.

    Opens or creates ``.engram/engram.db`` and initialises the server
    tables (buffer_items, dispatches, server_state).  The ``id_counters``
    table is left to :class:`engram.fold.ids.IDAllocator`.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        conn = self._connect()
        try:
            # Detect and rebuild legacy key-value server_state from migrate.py
            legacy_fold_from = self._migrate_legacy_server_state(conn)

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS buffer_items (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    path        TEXT NOT NULL,
                    item_type   TEXT NOT NULL,
                    chars       INTEGER NOT NULL DEFAULT 0,
                    date        TEXT,
                    drift_type  TEXT,
                    added_at    TEXT NOT NULL,
                    metadata    TEXT
                );

                CREATE TABLE IF NOT EXISTS dispatches (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_id    INTEGER NOT NULL,
                    state       TEXT NOT NULL DEFAULT 'building',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    input_path  TEXT,
                    prompt_path TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    error       TEXT
                );

                CREATE TABLE IF NOT EXISTS server_state (
                    id                  INTEGER PRIMARY KEY CHECK (id = 1),
                    last_poll_commit    TEXT,
                    last_poll_time      TEXT,
                    last_dispatch_time  TEXT,
                    buffer_chars_total  INTEGER NOT NULL DEFAULT 0,
                    last_session_mtime  REAL,
                    last_session_offset INTEGER NOT NULL DEFAULT 0,
                    last_session_tree_mtime REAL
                );
            """)

            # Add fold_from column (idempotent)
            try:
                conn.execute("ALTER TABLE server_state ADD COLUMN fold_from TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add l0_stale column (idempotent)
            try:
                conn.execute("ALTER TABLE server_state ADD COLUMN l0_stale INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute(
                    "ALTER TABLE server_state ADD COLUMN last_session_offset INTEGER DEFAULT 0",
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute(
                    "ALTER TABLE server_state ADD COLUMN last_session_tree_mtime REAL",
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Ensure singleton row exists
            conn.execute(
                "INSERT OR IGNORE INTO server_state (id, buffer_chars_total) VALUES (1, 0)"
            )

            # Restore fold_from from legacy migration if present
            if legacy_fold_from:
                conn.execute(
                    "UPDATE server_state SET fold_from = ? WHERE id = 1",
                    (legacy_fold_from,),
                )

            conn.commit()
        finally:
            conn.close()

    def _migrate_legacy_server_state(
        self, conn: sqlite3.Connection,
    ) -> str | None:
        """Detect and remove legacy key-value server_state schema.

        The legacy schema ``(key TEXT, value TEXT)`` was created by
        ``migrate.py:set_fold_marker()``.  If detected, extracts the
        ``fold_from`` value, drops the table, and returns the value so
        ``_init_tables`` can restore it after creating the correct schema.
        """
        rows = conn.execute("PRAGMA table_info(server_state)").fetchall()
        if not rows:
            return None  # Table doesn't exist yet
        col_names = {r[1] for r in rows}
        if "key" not in col_names or "id" in col_names:
            return None  # Already singleton schema

        # Legacy key-value schema — extract fold_from before dropping
        row = conn.execute(
            "SELECT value FROM server_state WHERE key = 'fold_from'"
        ).fetchone()
        legacy_fold_from = row[0] if row else None
        conn.execute("DROP TABLE server_state")
        conn.commit()
        return legacy_fold_from

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> ServerDB:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    # ------------------------------------------------------------------
    # Buffer items
    # ------------------------------------------------------------------

    def add_buffer_item(
        self,
        path: str,
        item_type: str,
        chars: int = 0,
        date: str | None = None,
        drift_type: str | None = None,
        metadata: str | None = None,
    ) -> int:
        """Insert a new buffer item. Returns the row id."""
        now = _now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """INSERT INTO buffer_items
                   (path, item_type, chars, date, drift_type, added_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (path, item_type, chars, date, drift_type, now, metadata),
            )
            # Update buffer_chars_total
            conn.execute(
                "UPDATE server_state SET buffer_chars_total = buffer_chars_total + ? WHERE id = 1",
                (chars,),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_buffer_items(self) -> list[dict[str, Any]]:
        """Return all pending buffer items ordered by date."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM buffer_items ORDER BY date, id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_buffer_chars(self) -> int:
        """Return total chars in the buffer."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT buffer_chars_total FROM server_state WHERE id = 1"
            ).fetchone()
            return row["buffer_chars_total"] if row else 0
        finally:
            conn.close()

    def clear_buffer(self) -> int:
        """Remove all buffer items. Returns count of items removed."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM buffer_items")
            count = cur.rowcount
            conn.execute(
                "UPDATE server_state SET buffer_chars_total = 0 WHERE id = 1"
            )
            conn.commit()
            return count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def consume_buffer(self, item_ids: list[int]) -> list[dict[str, Any]]:
        """Remove specific buffer items by id. Returns the consumed items.

        Atomically removes items and adjusts buffer_chars_total.
        """
        if not item_ids:
            return []
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in item_ids)
            rows = conn.execute(
                f"SELECT * FROM buffer_items WHERE id IN ({placeholders})",
                item_ids,
            ).fetchall()
            items = [dict(r) for r in rows]
            chars_removed = sum(item["chars"] for item in items)
            conn.execute(
                f"DELETE FROM buffer_items WHERE id IN ({placeholders})",
                item_ids,
            )
            conn.execute(
                "UPDATE server_state SET buffer_chars_total = MAX(0, buffer_chars_total - ?) WHERE id = 1",
                (chars_removed,),
            )
            conn.commit()
            return items
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def has_buffer_item(self, path: str) -> bool:
        """Check if a path is already in the buffer."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM buffer_items WHERE path = ? LIMIT 1",
                (path,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Dispatches
    # ------------------------------------------------------------------

    def create_dispatch(
        self,
        chunk_id: int,
        input_path: str | None = None,
        prompt_path: str | None = None,
    ) -> int:
        """Create a new dispatch record in 'building' state. Returns row id."""
        now = _now_iso()
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO dispatches
                   (chunk_id, state, created_at, updated_at, input_path, prompt_path)
                   VALUES (?, 'building', ?, ?, ?, ?)""",
                (chunk_id, now, now, input_path, prompt_path),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        finally:
            conn.close()

    def update_dispatch_state(
        self,
        dispatch_id: int,
        state: str,
        error: str | None = None,
    ) -> None:
        """Transition a dispatch to a new state."""
        if state not in DISPATCH_STATES:
            raise ValueError(f"Invalid dispatch state '{state}'. Must be one of {DISPATCH_STATES}")
        now = _now_iso()
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE dispatches
                   SET state = ?, updated_at = ?, error = ?
                   WHERE id = ?""",
                (state, now, error, dispatch_id),
            )
            conn.commit()
        finally:
            conn.close()

    def increment_retry(self, dispatch_id: int) -> int:
        """Increment retry count for a dispatch. Returns new count."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE dispatches SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
                (_now_iso(), dispatch_id),
            )
            row = conn.execute(
                "SELECT retry_count FROM dispatches WHERE id = ?",
                (dispatch_id,),
            ).fetchone()
            conn.commit()
            return row["retry_count"] if row else 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_dispatch(self, dispatch_id: int) -> dict[str, Any] | None:
        """Get a single dispatch record."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_non_terminal_dispatches(self) -> list[dict[str, Any]]:
        """Get all dispatches in non-terminal states (for crash recovery)."""
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in TERMINAL_STATES)
            rows = conn.execute(
                f"SELECT * FROM dispatches WHERE state NOT IN ({placeholders}) ORDER BY id",
                TERMINAL_STATES,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_recent_dispatches(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get the most recent dispatches for status display."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM dispatches ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_last_dispatch(self) -> dict[str, Any] | None:
        """Get the most recent dispatch."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM dispatches ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Server state
    # ------------------------------------------------------------------

    def get_server_state(self) -> dict[str, Any]:
        """Return the singleton server_state row."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM server_state WHERE id = 1"
            ).fetchone()
            return dict(row) if row else {"buffer_chars_total": 0}
        finally:
            conn.close()

    def set_fold_from(self, fold_date: str) -> None:
        """Set the fold_from marker (ISO date string)."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE server_state SET fold_from = ? WHERE id = 1",
                (fold_date,),
            )
            conn.commit()
        finally:
            conn.close()

    def get_fold_from(self) -> str | None:
        """Return the fold_from marker, or None if not set."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT fold_from FROM server_state WHERE id = 1"
            ).fetchone()
            return row["fold_from"] if row else None
        finally:
            conn.close()

    def clear_fold_from(self) -> None:
        """Clear the fold_from marker (set to NULL)."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE server_state SET fold_from = NULL WHERE id = 1"
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # L0 stale flag
    # ------------------------------------------------------------------

    def mark_l0_stale(self) -> None:
        """Set l0_stale = 1 to indicate briefing needs regeneration."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE server_state SET l0_stale = 1 WHERE id = 1"
            )
            conn.commit()
        finally:
            conn.close()

    def clear_l0_stale(self) -> None:
        """Set l0_stale = 0 after successful briefing regeneration."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE server_state SET l0_stale = 0 WHERE id = 1"
            )
            conn.commit()
        finally:
            conn.close()

    def is_l0_stale(self) -> bool:
        """Return True if L0 briefing needs regeneration."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT l0_stale FROM server_state WHERE id = 1"
            ).fetchone()
            return bool(row["l0_stale"]) if row else False
        finally:
            conn.close()

    def update_server_state(self, **kwargs: Any) -> None:
        """Update fields on the server_state singleton.

        Valid keys: last_poll_commit, last_poll_time, last_dispatch_time,
        buffer_chars_total, last_session_mtime.
        """
        valid_keys = {
            "last_poll_commit", "last_poll_time", "last_dispatch_time",
            "buffer_chars_total", "last_session_mtime",
            "last_session_offset", "last_session_tree_mtime",
        }
        invalid = set(kwargs) - valid_keys
        if invalid:
            raise ValueError(f"Invalid server_state keys: {invalid}")

        if not kwargs:
            return

        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values())
        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE server_state SET {set_clause} WHERE id = 1",
                values,
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def recover_on_startup(self) -> list[dict[str, Any]]:
        """Check for non-terminal dispatches and return them for recovery.

        Recovery strategy per state:
        - building: discard (rebuild from buffer)
        - dispatched: needs re-check or re-dispatch (returned to caller)
        - validated: mark L0 stale + committed (deferred to queue drain)

        Returns list of dispatch records needing attention.
        """
        non_terminal = self.get_non_terminal_dispatches()
        if not non_terminal:
            return []

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for d in non_terminal:
                if d["state"] == "building":
                    # Discard incomplete builds
                    conn.execute(
                        "DELETE FROM dispatches WHERE id = ?", (d["id"],)
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Return dispatched + validated records for the server to handle
        return [d for d in non_terminal if d["state"] in ("dispatched", "validated")]


def _now_iso() -> str:
    """Current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()
