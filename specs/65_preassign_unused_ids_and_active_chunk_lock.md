# Spec: Pre-assign unused IDs + single active chunk lock (Issue #65)

## Problem

1) Chunk generation can pre-assign stable IDs that already exist in the target project's living docs (C/E/W collision), leaving the fold agent without a usable pool for genuinely-new entries.

2) `engram next-chunk` is re-entrant. Calling it repeatedly can generate multiple chunk files before any are processed, making it easy for a background agent to skip earlier chunks.

## Goals

- Pre-assigned IDs are always **unused** relative to the target project's living docs.
- `engram next-chunk` produces **at most one active chunk** per project root unless explicitly cleared.
- Recovery path is explicit and obvious for low-capability agents.
- Behavior is test-covered and backward-compatible with existing `.engram/` state.

## Proposed changes

### A) Sync allocator counters to existing doc IDs

Before reserving IDs for a chunk, compute the maximum stable ID already present in the target project's living docs for each category (C/E/W) and bump the SQLite `id_counters.next_id` to at least `(max_seen + 1)` inside the same transaction used to reserve the ranges.

### B) Add active chunk lock for CLI `next-chunk`

- On successful chunk generation, write `.engram/active_chunk.yaml` with `{chunk_id, chunk_type, input_path, prompt_path, created_at}`.
- On subsequent `next-chunk` calls, refuse to generate a new chunk if the lock exists.
- Provide `engram clear-active-chunk` to delete the lock for recovery.
- Best-effort auto-clear: if git history includes a commit subject matching `Knowledge fold: chunk <id>`, clear the lock and proceed.

## Acceptance criteria

- Newly generated chunks never pre-assign IDs already present in living docs.
- Re-running `engram next-chunk` without clearing/completing the active chunk fails fast with a clear message.
- `engram clear-active-chunk` clears the lock and `next-chunk` works again.
- Unit tests cover both behaviors.

## Ticket classification

Single ticket.

