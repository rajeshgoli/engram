# Ticket 27 — Evidence commit dates for epistemic staleness

## Problem

`engram next-chunk` can enter an infinite drift-triage loop for `epistemic_audit` chunks when per-ID epistemic history is externalized into `docs/decisions/epistemic_state/E###.md`.

Specifically:
- The audit prompt requires appending history bullets in the format `- Evidence@<commit> <path>:<line>: <finding> -> believed|unverified`.
- That format does not contain a parseable date.
- Engram’s “staleness” logic still reports an old “last history” date for the entry, so the same stale epistemic entry is re-selected repeatedly.
- Drift triage rounds do not consume the chronological queue, so the system never progresses.

## Desired behavior

When evaluating epistemic entry recency based on external history files:
- Treat `Evidence@<commit>` bullets as timestamped by the referenced git commit date (author/commit time).
- Use the most recent recognized history event as the entry’s “last history” time.

## Approach

1. When parsing external epistemic history (`E###.md`):
   - If a line contains `Evidence@<sha>` (short or full), resolve `<sha>` in the target project’s git repo.
   - Use `git show -s --format=%ct <sha>` to obtain a Unix timestamp, then convert to a date.
2. Cache commit→timestamp resolution within a single engram run to avoid repeated git calls.
3. Maintain existing behavior for lines that already include an explicit date (if any).

## Acceptance criteria

- After appending an `Evidence@<commit>` bullet for an entry, that entry is no longer considered stale solely due to missing date text.
- `engram next-chunk` does not repeat the same `epistemic_audit` drift entry immediately after it was audited with an Evidence bullet.
- Unit tests cover:
  - Parsing Evidence bullets with short and full SHAs
  - Behavior when a commit cannot be resolved (ignored, does not crash)

## Classification

Single ticket.

