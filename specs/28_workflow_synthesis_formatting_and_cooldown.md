# Ticket 28 — Workflow synthesis formatting + safe cooldown

## Problems

### 1) Workflow list formatting collapses into one line

In `workflow_synthesis` triage chunks, the rendered list of workflows can collapse into a single line like:
`- **W001...** (W001)- **W002...** (W002)...`

This happens because the template uses `{% if %}...{% endif %}` inline and the Jinja environment trims
newlines after block tags, removing the newline between bullet items.

This is high-friction for low-capability/background agents.

### 2) Manifest-based dedup can suppress synthesis incorrectly

`next-chunk` currently writes `workflow_ids:` into `.engram/chunks_manifest.yaml` at chunk generation time.
Dedup logic then suppresses future `workflow_synthesis` drift if all CURRENT IDs are a subset of that list.

If a background fold agent aborts or fails to apply the synthesis edits, the manifest still claims those IDs
were “synthesized”, and engram may skip re-triggering synthesis even though nothing changed.

## Desired behavior

- Workflow lists in triage inputs render as newline-separated bullets.
- `workflow_synthesis` drift should avoid infinite loops, but should not assume synthesis “completed”
  just because a chunk was generated.

## Approach

1. Template fix: avoid inline `{% if %}` blocks on bullet lines. Use expression form:
   `{{ " (" ~ id ~ ")" if id else "" }}` so newlines are preserved under trim_blocks.
2. Replace “synthesized workflow IDs” dedup with a safe “cooldown” mechanism:
   - Record an *attempt marker* in `chunks_manifest.yaml` for `workflow_synthesis` that includes:
     - `workflow_registry_hash` (sha256 of the workflow registry file contents at generation time)
   - When considering `workflow_synthesis` drift:
     - If workflows are still over threshold and the registry hash hasn’t changed since the last attempt,
       suppress synthesis temporarily (cooldown in chunk IDs) and proceed consuming the queue.
     - Allow re-attempt after cooldown.

## Acceptance criteria

- A generated workflow_synthesis input shows one workflow per line.
- If workflows remain unchanged after a synthesis attempt, engram proceeds to normal fold chunks (no infinite drift loop),
  but can re-attempt after cooldown.
- Unit tests cover:
  - Formatting output contains newline-separated workflow bullets.
  - Cooldown suppression works when registry unchanged.
  - Re-attempt happens after cooldown or when registry changes.

## Classification

Single ticket.

