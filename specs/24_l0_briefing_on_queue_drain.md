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
- **Steady-state server:** buffer fills → dispatch → buffer empty → L0 regen. This is effectively after each dispatch since the server typically processes one chunk per cycle. The behavior only differs during multi-chunk bursts (busy development days with several dispatches queued).

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
`validated` state triggers L0 regen because the current state machine assumes validated → L0 regen → committed.

## Changes

### 1. Remove L0 regen from `Dispatcher.dispatch()`

The dispatcher's job is: build chunk → dispatch to agent → validate with linter. L0 is not part of that cycle.

```python
# dispatcher.py — after
if success:
    self._db.update_dispatch_state(dispatch_id, "validated")
    self._db.update_dispatch_state(dispatch_id, "committed")
    self._db.mark_l0_stale()  # signal that L0 needs regen
    ...
    return True
```

The `_regenerate_l0_briefing()` method stays on `Dispatcher` (it needs config + doc paths) but is no longer called internally. It becomes a public method called by the server loop and bootstrap fold.

### 2. Add `l0_stale` flag to `server_state` table

```sql
ALTER TABLE server_state ADD COLUMN l0_stale INTEGER DEFAULT 0;
```

- `mark_l0_stale()`: sets `l0_stale = 1`
- `clear_l0_stale()`: sets `l0_stale = 0`
- `is_l0_stale()`: returns boolean

### 3. Server loop: regen L0 on queue drain

In `run_server()`, after a dispatch succeeds, check if the buffer is empty. If empty and L0 is stale, regenerate.

```python
# server/__init__.py — in main loop
reason = buffer.should_dispatch()
if reason:
    success = dispatcher.dispatch()
    if success:
        # Check if buffer is drained
        if not buffer.should_dispatch() and db.is_l0_stale():
            dispatcher.regenerate_l0_briefing()
            db.clear_l0_stale()
```

### 4. Bootstrap fold: regen L0 once at the end

In `forward_fold()`, after all chunks are processed:

```python
# bootstrap/fold.py — at the end of forward_fold()
if chunk_count > 0 and failures == 0:
    log.info("Regenerating L0 briefing...")
    doc_paths = resolve_doc_paths(config, project_root)
    dispatcher = Dispatcher(config, project_root, db=None)
    dispatcher.regenerate_l0_briefing(doc_paths)
```

The `seed` CLI command should also regen after the seed dispatch completes (seed is a single dispatch that produces initial docs — that's a stable point).

### 5. Crash recovery update

The `validated` → `committed` recovery path no longer needs L0 regen. Instead, just mark committed and set `l0_stale`. The next server loop iteration will handle L0 when the buffer is drained.

```python
# dispatcher.py — recover_dispatch()
if dispatch["state"] == "validated":
    self._db.update_dispatch_state(dispatch_id, "committed")
    self._db.mark_l0_stale()
    return True
```

### 6. Update design doc

`engram_idea.md` dispatch mechanics step 7 currently says:
> If linter passes: update state → `validated`, regenerate L0 briefing (Haiku-class call), update state → `committed`

Change to:
> If linter passes: update state → `validated` → `committed`, mark L0 stale. L0 briefing regenerates when queue/buffer is fully drained (not per-dispatch).

## Files Changed

| File | Change |
|------|--------|
| `engram/server/dispatcher.py` | Remove L0 regen from `dispatch()`, make `regenerate_l0_briefing()` public, update `recover_dispatch()` |
| `engram/server/db.py` | Add `l0_stale` column, `mark_l0_stale()`, `clear_l0_stale()`, `is_l0_stale()` |
| `engram/server/__init__.py` | Add queue-drain L0 regen to server loop |
| `engram/bootstrap/fold.py` | Add L0 regen after all chunks complete |
| `engram/bootstrap/seed.py` | Add L0 regen after seed dispatch (check if it already does this) |
| `engram_idea.md` | Update dispatch mechanics step 7 |
| `tests/` | Update dispatcher tests, add queue-drain L0 timing tests |

## Acceptance

- During bootstrap fold of N chunks, L0 regen is called exactly once (after chunk N), not N times.
- During server steady-state, L0 regens after buffer drains, not after each dispatch.
- `engram seed` produces a briefing after the seed completes.
- Crash recovery: `validated` dispatch recovers to `committed` with `l0_stale` set, not with L0 regen.
- `python -m pytest tests/` passes.

## Ticket Classification

**Single ticket.** One agent, one context window.
