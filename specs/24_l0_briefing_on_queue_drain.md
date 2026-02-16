# L0 Briefing: Regenerate on Queue Drain, Not Per-Dispatch (#24)

**Status:** Spec
**Date:** 2026-02-16
**Design doc:** `engram_idea.md`

---

## Problem

L0 briefing currently regenerates after every successful dispatch (`dispatcher.py` line ~93). During bootstrap fold (10-20 chunks), this means:

1. **Agents get confidently wrong summaries.** Chunk 3 of 20 produces a briefing from 15% of the project's knowledge. An agent reading that CLAUDE.md briefing will trust it as authoritative, but it's missing 85% of the context — dead concepts not yet discovered, claims not yet refuted, relationships not yet surfaced.
2. **Wasted tokens.** Each L0 regen is a Haiku-class call (~$0.01-0.02). During a 20-chunk bootstrap that's 20 calls producing 19 throwaway briefings. Only the last one matters.

## Correct Semantic

L0 briefing should reflect **full knowledge of the project at a stable point**, not a work-in-progress snapshot. It should regenerate when the queue/buffer is drained — the moment there's nothing left to process.

- **Bootstrap fold:** many chunks → L0 regen once at the end, after all chunks complete.
- **Steady-state server:** buffer triggers dispatch → dispatch consumes queue → queue empty → L0 regen. This is effectively after each dispatch since the server typically processes one chunk per cycle. The behavior only differs during multi-chunk bursts (busy development days with several dispatches queued).

## Current Implementation

Three code paths touch L0:

### 1. `engram/server/dispatcher.py` — `Dispatcher.dispatch()`
```python
if success:
    self._db.update_dispatch_state(dispatch_id, "validated")
    self._regenerate_l0_briefing(doc_paths)  # ← HERE: after every dispatch
    self._db.update_dispatch_state(dispatch_id, "committed")
```

### 2. `engram/bootstrap/fold.py` — `forward_fold()`
No L0 regen at all. Processes all chunks, returns. The caller (CLI `seed` or `fold` command) doesn't regen either.

### 3. `engram/server/__init__.py` — `run_server()` loop
Calls `dispatcher.dispatch()` which includes L0 regen internally.

### 4. Crash recovery — `Dispatcher.recover_dispatch()`
Three L0 regen sites: (a) `validated` state triggers L0 regen (line 250), (b) `dispatched` → re-lint passes → L0 regen (line 273), (c) `dispatched` → retry succeeds → L0 regen (line 300). All assume validated → L0 regen → committed.

## Changes

### 1. Remove L0 regen from `Dispatcher.dispatch()`

The dispatcher's job is: build chunk → dispatch to agent → validate with linter. L0 is not part of that cycle.

```python
# dispatcher.py — after
if success:
    self._db.update_dispatch_state(dispatch_id, "validated")
    self._db.mark_l0_stale()  # BEFORE committed — crash-safe ordering
    self._db.update_dispatch_state(dispatch_id, "committed")
    ...
    return True
```

**Ordering invariant:** `mark_l0_stale()` MUST precede `update_dispatch_state(committed)`. If the process crashes between the two, the dispatch remains non-terminal (`validated`), recovery picks it up, transitions to `committed`, and `l0_stale` is already set. The reverse order (committed first, then stale) would leave a terminal dispatch with no stale flag — unrecoverable.

The L0 regen logic is extracted from `Dispatcher` into a standalone function (see Change #6). Dispatcher no longer owns or calls it.

### 2. Add `l0_stale` flag to `server_state` table

New column on the `server_state` singleton:

```sql
ALTER TABLE server_state ADD COLUMN l0_stale INTEGER DEFAULT 0;
```

**Idempotent migration:** Existing databases won't have this column. Add the ALTER in `ServerDB._init_tables()` guarded by `try/except sqlite3.OperationalError` (the standard SQLite pattern — `ALTER TABLE ADD COLUMN IF NOT EXISTS` requires SQLite 3.35+ which isn't guaranteed):

```python
try:
    conn.execute("ALTER TABLE server_state ADD COLUMN l0_stale INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass  # Column already exists
```

New `ServerDB` methods:
- `mark_l0_stale()`: sets `l0_stale = 1`
- `clear_l0_stale()`: sets `l0_stale = 0`
- `is_l0_stale() -> bool`: returns boolean

### 3. Server loop: regen L0 on queue drain

**Drain invariant:** "drain" is evaluated against the same state source that the dispatch cycle consumes — `queue.jsonl` via the chunker, NOT `buffer_items` or `buffer.should_dispatch()`. The buffer drives dispatch *triggering*; the queue drives dispatch *content*. These are different state sources and can diverge.

Add `queue_is_empty(project_root) -> bool` to `engram/fold/chunker.py`: returns True if `queue.jsonl` is missing, empty, or has zero entries. This is the drain predicate.

In `run_server()`, two L0 check sites:

```python
# server/__init__.py

# (A) On startup, after crash recovery completes:
if db.is_l0_stale() and queue_is_empty(project_root):
    doc_paths = resolve_doc_paths(config, project_root)
    if regenerate_l0_briefing(config, project_root, doc_paths):
        db.clear_l0_stale()

# (B) In main loop — unconditional L0 check, outside dispatch block:
reason = buffer.should_dispatch()
if reason:
    success = dispatcher.dispatch()
    ...

# Top-level: runs every iteration, not nested inside dispatch
if db.is_l0_stale() and queue_is_empty(project_root):
    doc_paths = resolve_doc_paths(config, project_root)
    if regenerate_l0_briefing(config, project_root, doc_paths):
        db.clear_l0_stale()
    # If regen fails, l0_stale stays set — next iteration retries
```

This fixes two issues: (a) starvation on quiet startup (L0 check runs independently of dispatch), (b) drain predicate tracks the dispatch source (queue), not the trigger source (buffer).

**Note:** The buffer→queue pipeline (how `buffer_items` become `queue.jsonl` entries in server mode) is a pre-existing architectural question outside this ticket's scope. This ticket's invariant is: L0 regens when the dispatch source (queue) is drained.

### 4. Bootstrap fold: regen L0 once at the end

In `forward_fold()`, after all chunks are processed:

```python
# bootstrap/fold.py — at the end of forward_fold()
if chunk_count > 0 and failures == 0:
    log.info("Regenerating L0 briefing...")
    doc_paths = resolve_doc_paths(config, project_root)
    regenerate_l0_briefing(config, project_root, doc_paths)
```

`regenerate_l0_briefing` is extracted as a standalone function (not a Dispatcher method) since bootstrap fold doesn't instantiate a Dispatcher. It needs only config (for model selection) and doc paths (to read living docs). The current implementation (`_regenerate_l0_briefing` on Dispatcher) only uses `self._config` and `self._project_root` — no db access — so extraction is clean.

**`seed.py`** also needs L0 regen. Confirmed: `seed.py` currently does NOT regen L0. Add a call after `_dispatch_seed_agent` succeeds (before fold-forward for Path A, since the seed itself is a stable point):

```python
# bootstrap/seed.py — in seed(), after _dispatch_seed_agent succeeds
if success:
    doc_paths = resolve_doc_paths(config, project_root)
    regenerate_l0_briefing(config, project_root, doc_paths)
```

For Path A (seed + fold forward), this produces two L0 regens: one after seed, one after fold completes. The post-seed regen is useful if fold forward fails partway — at least the seed knowledge is briefed.

### 5. Crash recovery update

All three L0 regen sites in `recover_dispatch()` are replaced with `mark_l0_stale()`:

```python
# dispatcher.py — recover_dispatch()
# Same ordering invariant: mark_l0_stale() BEFORE committed.

# (a) validated state
if dispatch["state"] == "validated":
    self._db.mark_l0_stale()
    self._db.update_dispatch_state(dispatch_id, "committed")
    return True

# (b) dispatched → re-lint passes
if result.passed:
    self._db.update_dispatch_state(dispatch_id, "validated")
    self._db.mark_l0_stale()
    self._db.update_dispatch_state(dispatch_id, "committed")
    return True

# (c) dispatched → retry succeeds
if result2.passed:
    self._db.update_dispatch_state(dispatch_id, "validated")
    self._db.mark_l0_stale()
    self._db.update_dispatch_state(dispatch_id, "committed")
    return True
```

All recovery paths defer L0 to the server loop's drain check.

### 6. Extract `regenerate_l0_briefing` as standalone function

Move from `Dispatcher._regenerate_l0_briefing()` to a module-level function (e.g., in `engram/briefing.py` or `engram/server/briefing.py`). Signature:

```python
def regenerate_l0_briefing(config: dict, project_root: Path, doc_paths: dict) -> bool:
```

Returns True on success, False on failure. Dispatcher's `dispatch()` no longer calls it. Server loop and bootstrap paths call it directly. This avoids bootstrap needing to instantiate a Dispatcher just for L0.

### 7. Update design doc

Three updates to `engram_idea.md`:

**(a) Dispatch mechanics step 7** (line 119) currently says:
> If linter passes: update state → `validated`, regenerate L0 briefing (Haiku-class call), update state → `committed`

Change to:
> If linter passes: update state → `validated`, mark L0 stale, update state → `committed`. L0 briefing regenerates when the dispatch queue is fully drained (not per-dispatch).

**(b) Crash recovery section** (lines 123-127) — `validated` recovery currently says L0 regen didn't complete. Change to:
> `validated` → mark committed, set L0 stale (deferred to next drain)

**(c) Tiered Retrieval** (line 155) currently says:
> Auto-regenerated after each dispatch.

Change to:
> Auto-regenerated when dispatch queue drains (not per-dispatch).

## Files Changed

| File | Change |
|------|--------|
| `engram/server/dispatcher.py` | Remove L0 regen from `dispatch()` and all 3 sites in `recover_dispatch()`, replace with `mark_l0_stale()` (stale before committed ordering) |
| `engram/server/db.py` | Add `l0_stale` column (idempotent migration), `mark_l0_stale()`, `clear_l0_stale()`, `is_l0_stale()` |
| `engram/server/__init__.py` | Add queue-drain L0 regen: startup check + unconditional main-loop check |
| `engram/server/briefing.py` (new) | Extract `regenerate_l0_briefing()` as standalone function |
| `engram/fold/chunker.py` | Add `queue_is_empty(project_root) -> bool` drain predicate |
| `engram/bootstrap/fold.py` | Add L0 regen after all chunks complete |
| `engram/bootstrap/seed.py` | Add L0 regen after seed dispatch (confirmed: currently has none) |
| `engram_idea.md` | Update dispatch mechanics step 7, crash recovery section, Tiered Retrieval L0 description |
| `tests/` | Update dispatcher tests, add queue-drain L0 timing tests, test crash-window ordering |

## Acceptance

- During bootstrap fold of N chunks, L0 regen is called exactly once (after chunk N), not N times.
- During server steady-state, L0 regens when dispatch queue drains (`queue_is_empty`), not after each dispatch.
- On quiet startup (no new dispatches after recovery), stale L0 is regenerated by the startup check.
- `engram seed` produces a briefing after the seed completes.
- Crash recovery: all 3 recovery paths (`validated`, `dispatched`→lint pass, `dispatched`→retry pass) use `mark_l0_stale()`, not direct L0 regen.
- Crash safety: `mark_l0_stale()` always precedes `update_dispatch_state(committed)`. A crash between the two leaves a recoverable (non-terminal) dispatch with stale flag already set.
- If L0 regen fails (Haiku timeout, etc.), `l0_stale` remains set so the next iteration retries.
- Existing databases gain `l0_stale` column via idempotent migration (no manual intervention).
- `python -m pytest tests/` passes.

## Ticket Classification

**Single ticket.** One agent, one context window.
