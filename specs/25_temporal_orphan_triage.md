# Temporal Orphan Triage: Check Fold-From Date, Not Today (#25)

**Status:** Spec
**Date:** 2026-02-16
**Design doc:** `engram_idea.md`

---

## Problem

Orphan detection and triage are temporally wrong during fold-forward.

### Detection: `_find_orphaned_concepts()` checks today's filesystem

`_find_orphaned_concepts()` in `engram/fold/chunker.py:91-113` checks `(project_root / p).exists()` against the current working tree. When `fold_from` is set (e.g., 2026-01-01 after migration), living docs only know about events through that date. A file renamed on Feb 1 appears "orphaned" even though the docs haven't processed any events past Jan 1.

Consequence: the fold agent marks concepts DEAD based on Feb 16 filesystem state, then processes Jan 2–31 queue items that reference the old paths as active code. The living docs become inconsistent — concepts die before they're born.

This also affects the orphan advisory in normal fold chunks (`chunker.py:394-404`), which uses the same `_find_orphaned_concepts()` output.

### Agent prompt: no temporal context

The triage prompt (`templates/triage_prompt.md:17-43`) tells the agent to check whether source files exist but gives no date or commit context. The agent inspects today's filesystem and reaches the same wrong conclusions as the detection code.

### Schema conflict: `fold_from` storage is broken

`migrate.py:set_fold_marker()` creates `server_state` with a key-value schema `(key TEXT, value TEXT)`. `server/db.py:ServerDB._init_tables()` creates `server_state` with a singleton-row schema `(id INTEGER, last_poll_commit TEXT, ...)`. Both use `CREATE TABLE IF NOT EXISTS`, so whichever runs first wins. Whichever runs second silently succeeds — then its queries fail because the columns don't exist.

This must be reconciled for `fold_from` to be readable by both migrate and chunker code paths.

## Current Behavior

1. `engram migrate --fold-from 2026-01-01` sets marker via `set_fold_marker()` (key-value schema)
2. `engram next-chunk` calls `next_chunk()` → `scan_drift()` → `_find_orphaned_concepts()`
3. `_find_orphaned_concepts()` checks `os.path.exists()` against today's filesystem
4. 63 orphans detected → drift triage chunk produced before any queue items
5. Triage prompt lists orphans but gives agent no temporal context
6. Agent resolves orphans based on Feb 16 state; subsequent Jan queue items contradict

## Changes

### 1. Reconcile `fold_from` storage — add column to `server_state` with legacy migration

Add `fold_from TEXT` to `db.py`'s singleton-row schema. This requires handling two cases:

**Case A — fresh DB or existing singleton schema:** Idempotent ALTER (same pattern as #24's `l0_stale`):

```python
# server/db.py — _init_tables()
try:
    conn.execute("ALTER TABLE server_state ADD COLUMN fold_from TEXT")
except sqlite3.OperationalError:
    pass  # Column already exists
```

**Case B — legacy key-value schema from prior `engram migrate`:** The existing `server_state(key TEXT, value TEXT)` table is incompatible with `ServerDB`'s singleton-row schema. `CREATE TABLE IF NOT EXISTS` silently succeeds on either shape, then subsequent queries fail because the columns don't match.

`_init_tables()` must detect and rebuild the legacy shape:

```python
# server/db.py — _init_tables(), before CREATE TABLE IF NOT EXISTS
def _has_legacy_server_state(self, conn: sqlite3.Connection) -> bool:
    """Detect key-value schema from migrate.py set_fold_marker()."""
    rows = conn.execute("PRAGMA table_info(server_state)").fetchall()
    if not rows:
        return False  # Table doesn't exist
    col_names = {r[1] for r in rows}  # r[1] is column name
    return "key" in col_names and "id" not in col_names
```

When legacy shape is detected:

```python
# In _init_tables():
if self._has_legacy_server_state(conn):
    # Preserve fold_from value before rebuild
    row = conn.execute(
        "SELECT value FROM server_state WHERE key = 'fold_from'"
    ).fetchone()
    legacy_fold_from = row[0] if row else None

    conn.execute("DROP TABLE server_state")
    # CREATE TABLE + singleton INSERT proceeds normally below
    # ... after table creation:
    if legacy_fold_from:
        conn.execute(
            "UPDATE server_state SET fold_from = ? WHERE id = 1",
            (legacy_fold_from,),
        )
```

This preserves the `fold_from` value across the schema rebuild. The legacy table is only created by `set_fold_marker()` in `migrate.py`, and only contains `fold_from` — no other data to preserve.

New `ServerDB` methods:
- `set_fold_from(fold_date: str) -> None` — sets `fold_from` column
- `get_fold_from() -> str | None` — returns ISO date string or None
- `clear_fold_from() -> None` — sets `fold_from = NULL`

### 2. Rewrite `migrate.py:set_fold_marker()` to use `ServerDB`

Replace the standalone key-value table creation with `ServerDB`:

```python
# migrate.py
def set_fold_marker(db_path: Path, fold_from: date) -> None:
    from engram.server.db import ServerDB
    db = ServerDB(db_path)
    db.set_fold_from(fold_from.isoformat())
```

This ensures the table is always created with the correct singleton-row schema. `ServerDB.__init__` calls `_init_tables()` which handles both fresh creation, legacy migration, and the `fold_from` column addition.

### 3. Add git-based file existence checking

New helper in `engram/fold/chunker.py`:

```python
def _resolve_ref_commit(project_root: Path, fold_from: str) -> str | None:
    """Resolve a fold_from date to the nearest git commit hash.

    Uses `git log --before=<date+1day> -1 --format=%H` to find the
    latest commit on or before the fold_from date.

    Returns the commit hash, or None if no commit found.
    """
```

```python
def _file_exists_at_commit(project_root: Path, ref_commit: str, path: str) -> bool:
    """Check if a file exists at a specific git commit.

    Uses `git ls-tree <commit> -- <path>` — exit code 0 with output
    means the file exists.
    """
```

These are pure `subprocess.run` calls with `capture_output=True`. No worktree needed for detection — `git ls-tree` reads the object store directly.

### 4. Update `_find_orphaned_concepts()` to accept temporal reference

```python
def _find_orphaned_concepts(
    concepts_path: Path,
    project_root: Path,
    ref_commit: str | None = None,  # NEW
) -> list[dict]:
```

When `ref_commit` is None (steady-state): use `os.path.exists()` (current behavior, unchanged).

When `ref_commit` is set (fold-forward): use `_file_exists_at_commit()` for each code path. Only flag as orphaned if the file was already missing at the reference commit.

### 5. Thread `fold_from` through `scan_drift()` and `next_chunk()`

```python
def scan_drift(
    config: dict,
    project_root: Path,
    fold_from: str | None = None,  # NEW: ISO date string
) -> DriftReport:
```

If `fold_from` is set: resolve to `ref_commit` via `_resolve_ref_commit()`, pass to `_find_orphaned_concepts()`. If resolution fails (no commit before that date), fall back to filesystem check with a warning log.

```python
def next_chunk(
    config: dict,
    project_root: Path,
    fold_from: str | None = None,  # NEW
) -> ChunkResult:
```

Passes `fold_from` through to `scan_drift()`.

### 6. Update callers to pass `fold_from`

**`bootstrap/fold.py` — `forward_fold()`:**

Already has `from_date`. Pass it to `next_chunk()`:

```python
chunk = next_chunk(config, project_root, fold_from=from_date.isoformat())
```

Clear the marker on **both** success paths:

```python
# Early return — filtered queue is empty (fold.py:171-173)
if remaining == 0:
    log.info("No entries to process after %s", from_date)
    db = ServerDB(project_root / ".engram" / "engram.db")
    db.clear_fold_from()
    return True

# ... main loop ...

# Normal completion — all chunks processed
if not failures:
    db = ServerDB(project_root / ".engram" / "engram.db")
    db.clear_fold_from()
```

Without clearing on the early-return path, a `fold_from` marker set by migration followed by an empty queue (no artifacts after the fold date) would leave temporal mode enabled permanently — all future orphan checks would reference the stale commit.

**`cli.py` — `next_chunk_cmd()`:**

Read `fold_from` from SQLite before calling `next_chunk()`:

```python
from engram.server.db import ServerDB

db = ServerDB(root / ".engram" / "engram.db")
fold_from = db.get_fold_from()
result = next_chunk(config, root, fold_from=fold_from)
```

**`server/buffer.py` — `ContextBuffer.should_dispatch()`:**

Currently calls `scan_drift(self._config, self._project_root)` at line 79 without `fold_from`. Wire it through:

```python
fold_from = self._db.get_fold_from()
drift = scan_drift(self._config, self._project_root, fold_from=fold_from)
```

`ContextBuffer` already holds `self._db` (a `ServerDB` instance), so no new dependencies.

**`server/dispatcher.py` — `Dispatcher.dispatch()`:**

Currently calls `next_chunk(self._config, self._project_root)` at line 69 without `fold_from`. Wire it through:

```python
fold_from = self._db.get_fold_from()
chunk = next_chunk(self._config, self._project_root, fold_from=fold_from)
```

`Dispatcher` already holds `self._db`, same as buffer.

In practice the server shouldn't be running during fold-forward, but if `fold_from` is set (e.g., migration completed but fold hasn't run yet), these paths must respect it rather than silently producing incorrect orphan triage.

### 7. Add temporal context to triage prompt

**`fold/prompt.py` — `render_triage_input()`:**

Add optional temporal context parameters:

```python
def render_triage_input(
    *,
    drift_type: str,
    drift_report: Any,
    chunk_id: int,
    doc_paths: dict[str, Path],
    ref_commit: str | None = None,   # NEW
    ref_date: str | None = None,     # NEW
) -> str:
```

Pass `ref_commit` and `ref_date` to the template.

**`templates/triage_prompt.md` — orphan triage section:**

Add a conditional temporal context block after the orphan list:

```jinja2
{% if ref_commit %}
## Temporal Context

Living docs are current through **{{ ref_date }}** (commit `{{ ref_commit[:12] }}`).
Check file existence at that commit, NOT today's filesystem.

To inspect files at the reference point:
```
git worktree add /tmp/engram-triage-{{ ref_commit[:8] }} {{ ref_commit }}
```

Check paths in that worktree. When done:
```
git worktree remove /tmp/engram-triage-{{ ref_commit[:8] }}
```

If a file exists at that commit but is missing today, it was renamed/moved AFTER
the date living docs know about — leave it ACTIVE. The fold will process the
rename when it reaches that date in the queue.
{% endif %}
```

When `ref_commit` is None (steady-state triage), this block is omitted and the prompt is unchanged from today.

### 8. Pass temporal context from `next_chunk()` to prompt

In `next_chunk()`, when building a drift triage chunk with `fold_from` set:

```python
ref_commit = None
if fold_from:
    ref_commit = _resolve_ref_commit(project_root, fold_from)

# ... existing drift triage chunk building ...

input_content = render_triage_input(
    drift_type=drift_type,
    drift_report=drift,
    chunk_id=chunk_id,
    doc_paths=doc_paths,
    ref_commit=ref_commit,      # NEW
    ref_date=fold_from,         # NEW
)
```

Same for the orphan advisory in normal fold chunks — include `ref_date` in the advisory text so the fold agent knows the temporal context.

## Files Changed

| File | Change |
|------|--------|
| `engram/server/db.py` | Legacy key-value schema detection + rebuild in `_init_tables()`. Add `fold_from` column (idempotent ALTER). Add `set_fold_from()`, `get_fold_from()`, `clear_fold_from()`. |
| `engram/migrate.py` | Rewrite `set_fold_marker()` to use `ServerDB` instead of standalone key-value table |
| `engram/fold/chunker.py` | Add `_resolve_ref_commit()`, `_file_exists_at_commit()`. Add `ref_commit` param to `_find_orphaned_concepts()`. Add `fold_from` param to `scan_drift()` and `next_chunk()`. |
| `engram/fold/prompt.py` | Add `ref_commit`, `ref_date` params to `render_triage_input()` |
| `engram/templates/triage_prompt.md` | Add conditional temporal context block for orphan triage |
| `engram/bootstrap/fold.py` | Pass `from_date` as `fold_from` to `next_chunk()`. Clear `fold_from` on both success paths (early empty-queue return + normal completion). |
| `engram/cli.py` | `next-chunk` command reads `fold_from` from db and passes to `next_chunk()` |
| `engram/server/buffer.py` | `should_dispatch()` reads `fold_from` from db and passes to `scan_drift()` |
| `engram/server/dispatcher.py` | `dispatch()` reads `fold_from` from db and passes to `next_chunk()` |
| `tests/` | Test git-based orphan detection, temporal prompt rendering, fold_from lifecycle (set → use → clear), legacy schema migration, zero-work fold_from clearing |

## Acceptance

- During fold-forward with `fold_from=2026-01-01`, orphan detection checks file existence at the Jan 1 commit, not today's filesystem. Files renamed after Jan 1 are NOT flagged as orphaned.
- Orphan triage prompt includes commit hash, date, and worktree instructions when `fold_from` is set.
- In steady-state (no `fold_from`), orphan detection uses `os.path.exists()` — behavior unchanged.
- After successful `forward_fold()` completion, `fold_from` is cleared — including the early-return path when filtered queue is empty. Subsequent orphan checks use filesystem.
- `ServerDB._init_tables()` detects legacy `server_state(key, value)` schema, rebuilds to singleton-row schema, and preserves the `fold_from` value across the migration.
- Databases that already have the singleton schema gain `fold_from` column via idempotent ALTER — no data loss.
- `set_fold_marker()` uses `ServerDB` — no schema conflict between `migrate.py` and `db.py`.
- If `git log` can't resolve the `fold_from` date to a commit (e.g., date predates repo), detection falls back to filesystem check with a warning.
- Orphan advisory in normal fold chunks also respects `fold_from` temporal context.
- Server paths (`buffer.py:should_dispatch()`, `dispatcher.py:dispatch()`) respect `fold_from` when set.
- `python -m pytest tests/` passes.

## Ticket Classification

**Single ticket.** One agent, one context window.
