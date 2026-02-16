# build-queue Ignores fold-from Marker (#26)

**Status:** Spec
**Date:** 2026-02-16
**Design doc:** `engram_idea.md`

---

## Problem

`build_queue()` in `engram/fold/queue.py` scans all project artifacts and builds the full chronological queue regardless of the `fold_from` marker in `engram.db`. After migrating v2 docs current through 2026-01-01, `engram build-queue` produces 4,001 entries from 2025-01-19 — 11 months of already-processed content.

`forward_fold()` partially mitigates this with `_filter_queue_by_date()` (bootstrap/fold.py:32-58), but:

1. **CLI `build-queue` is unfiltered.** Running it standalone produces a queue file with all artifacts. A subsequent `engram next-chunk` processes pre-fold content.
2. **Session files are written unnecessarily.** `build_queue()` writes all session markdown files to `.engram/sessions/` (queue.py:160-161) before any filtering. For large histories this is wasted I/O.
3. **Two-step filter is fragile.** Building everything then discarding most of it is wasteful and relies on callers remembering to filter.

## Current Behavior

1. `engram migrate --fold-from 2026-01-01` stores marker in DB
2. `engram build-queue` calls `build_queue(config, root)` — outputs 4,001 entries from 2025-01-19 to 2026-02-16
3. `engram next-chunk` produces chunk covering Jan 19 – Dec 12, 2025 — re-folds content v2 already processed
4. `forward_fold()` builds full queue (line 166), then filters it in a separate step (line 170) — correct but wasteful

## Changes

### 1. Add `start_date` parameter to `build_queue()`

```python
def build_queue(
    config: dict[str, Any],
    project_root: Path,
    output_dir: Path | None = None,
    start_date: str | None = None,  # NEW: YYYY-MM-DD string only
) -> list[dict[str, Any]]:
```

**`start_date` must be a `YYYY-MM-DD` string** (10 chars). The filter compares `e["date"][:10] >= start_date`, which is a prefix comparison between two 10-char date strings. Passing a full ISO datetime (e.g., `2026-01-01T00:00:00`) would silently exclude same-day entries because `'2026-01-01' >= '2026-01-01T00:00:00'` is `False` in Python string ordering. All callers must normalize to `YYYY-MM-DD` before passing.

When `start_date` is set, filter after sorting and before writing:

```python
# After line 177: entries.sort(key=lambda e: e["date"])
if start_date:
    date.fromisoformat(start_date)  # Validates YYYY-MM-DD; raises ValueError otherwise
    entries = [e for e in entries if e["date"][:10] >= start_date]
```

This is the same comparison `_filter_queue_by_date()` uses (ISO string prefix comparison), applied at the source. The queue.jsonl file and returned list contain only post-cutoff entries.

**Session file optimization:** Move session file writing after the date filter so only relevant sessions are written to disk. Currently session files are written at line 160-161 (inside the loop), before any filtering. Restructure to:

1. Collect session entries in memory (with rendered content)
2. Apply date filter to all entries (docs + issues + sessions)
3. Write only the session files for entries that survived the filter

```python
# Collect sessions without writing files yet
pending_sessions: list[tuple[dict, str, str]] = []  # (entry, session_id, rendered)
for se in session_entries:
    rel_path = str(sessions_dir / f"{se.session_id}.md")
    pending_sessions.append(({
        "date": se.date,
        "type": "prompts",
        "path": rel_path,
        ...
    }, se.session_id, se.rendered))
    entries.append(pending_sessions[-1][0])

# Sort + filter
entries.sort(key=lambda e: e["date"])
if start_date:
    entries = [e for e in entries if e["date"][:10] >= start_date]

# Write only surviving session files
surviving_paths = {e["path"] for e in entries if e["type"] == "prompts"}
for entry, session_id, rendered in pending_sessions:
    if entry["path"] in surviving_paths:
        (sessions_dir / f"{session_id}.md").write_text(rendered)
```

### 2. CLI `build-queue` reads `fold_from` and passes it

```python
# cli.py — build_queue_cmd()
from engram.server.db import ServerDB

root = Path(project_root)
config = load_config(root)

db = ServerDB(root / ".engram" / "engram.db")
fold_from = db.get_fold_from()

entries = build_queue(config, root, start_date=fold_from)
```

Depends on `ServerDB.get_fold_from()` from #25. If `fold_from` is None (no marker), behavior is unchanged — full queue built.

### 3. Simplify `forward_fold()` — pass `from_date` to `build_queue()`

`forward_fold()` currently builds the full queue then filters in a separate step:

```python
# Current (fold.py:164-170)
entries = build_queue(config, project_root)       # Full queue
remaining = _filter_queue_by_date(project_root, from_date)  # Then filter
```

With `start_date` on `build_queue()`, this becomes:

```python
entries = build_queue(config, project_root, start_date=from_date.isoformat())
remaining = len(entries)
```

`_filter_queue_by_date()` becomes unused. Remove the function and its tests (`test_bootstrap.py:265-294`).

**Scope boundary with #25:** This change only touches queue-building and filtering logic in `forward_fold()`. #25 adds other behavior to the same function — passing `fold_from=from_date.isoformat()` to `next_chunk()` calls and clearing the `fold_from` marker on both success paths (early empty-queue return + normal completion). Those semantics must be preserved. When resolving merge conflicts between #25 and #26, keep #25's `next_chunk(..., fold_from=...)` and marker lifecycle intact; only the queue-building/filtering section changes.

### 4. Add `--start-date` CLI option to `build-queue`

Allow explicit override independent of the DB marker:

```python
@cli.command("build-queue")
@click.option("--start-date", type=click.DateTime(formats=["%Y-%m-%d"]),
              default=None, help="Only include entries on or after this date.")
@click.option("--project-root", ...)
def build_queue_cmd(project_root: str, start_date: object) -> None:
```

**Canonicalization:** `click.DateTime` returns a `datetime` object. Normalize to `YYYY-MM-DD` before passing to `build_queue()`:

```python
# Resolve start_date: explicit flag > DB marker > None
if start_date:
    effective_start = start_date.date().isoformat()  # datetime → "YYYY-MM-DD"
elif fold_from:
    effective_start = fold_from  # Already YYYY-MM-DD from ServerDB
else:
    effective_start = None

entries = build_queue(config, root, start_date=effective_start)
```

Priority: explicit `--start-date` flag > `fold_from` in DB > None (full queue). This lets users override the marker for testing or manual runs.

## Files Changed

| File | Change |
|------|--------|
| `engram/fold/queue.py` | Add `start_date` param to `build_queue()` (YYYY-MM-DD only, validated via `date.fromisoformat`). Filter entries after sort. Defer session file writes to after filtering. `item_sizes.json` remains unfiltered (intentional — full artifact inventory for debugging). |
| `engram/cli.py` | `build-queue` command: add `--start-date` option, read `fold_from` from DB as default. |
| `engram/bootstrap/fold.py` | Pass `from_date` as `start_date` to `build_queue()`. Remove `_filter_queue_by_date()`. |
| `tests/test_bootstrap.py` | Remove `_filter_queue_by_date` tests. Add tests for `build_queue(start_date=...)` filtering. |
| `tests/test_queue.py` | Add tests for `start_date` filtering: entries before date excluded, session files only written for surviving entries. |

## Dependency

Requires #25 for `ServerDB.get_fold_from()`. If #25 is not yet merged, the CLI integration (Change 2) can be deferred — the core `build_queue(start_date=...)` parameter and `forward_fold()` simplification are independent.

## Acceptance

- `build_queue(config, root, start_date="2026-01-01")` returns only entries with `date >= "2026-01-01"`. Queue.jsonl contains only post-cutoff entries.
- Session markdown files are only written for entries that survive the date filter.
- `engram build-queue` with `fold_from` set in DB produces a filtered queue automatically.
- `engram build-queue --start-date 2026-01-01` overrides any DB marker.
- `forward_fold()` uses `start_date` on `build_queue()` instead of separate `_filter_queue_by_date()` step.
- `_filter_queue_by_date()` is removed (dead code after this change).
- Without `fold_from` or `--start-date`, behavior is unchanged — full queue built.
- `item_sizes.json` remains a full unfiltered inventory of all artifacts (not gated by `start_date`).
- `start_date` parameter rejects non-`YYYY-MM-DD` strings (validated via `date.fromisoformat`, guards against silent same-day exclusion from datetime ISO strings).
- `python -m pytest tests/` passes.

## Ticket Classification

**Single ticket.** One agent, one context window.
