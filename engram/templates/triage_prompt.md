# Instructions

You are updating 4 knowledge documents to resolve drift.

Living docs:
- **{{ doc_paths.timeline }}** — Chronological narrative.
- **{{ doc_paths.concepts }}** — Concept registry (C###).
- **{{ doc_paths.epistemic }}** — Epistemic state (E###).
- **{{ doc_paths.workflows }}** — Workflow registry (W###).

Graveyard files (append-only):
- {{ doc_paths.concept_graveyard }}
- {{ doc_paths.epistemic_graveyard }}

---

{% if drift_type == "orphan_triage" %}
# Orphan Triage Round (Chunk {{ chunk_id }})

This is a dedicated cleanup round. No new content to process.
Your ONLY job is to triage the orphaned concepts below.

## [ORPHANED CONCEPTS] Active concepts with missing source files

The following concepts are marked ACTIVE but ALL their referenced source files
no longer exist. All source trees are in the same repo — "different source tree"
is never a valid reason to skip triage. For each one:

- Mark **DEAD** if the concept was abandoned/replaced.
  Use 1-2 sentences: what replaced it + key lesson.
  Move the full entry to {{ doc_paths.concept_graveyard }}, replace with STUB.
- Mark **EVOLVED** if it was renamed/restructured.
  Change the heading to: `## C{NNN}: name (EVOLVED) → new_name` (arrow OUTSIDE parens).
  Update the Code: field to point to the new location. Keep the body in place.
  Do NOT move EVOLVED entries to the graveyard — graveyard is for DEAD only.
  (Common renames: .ts→.tsx, ground_truth_annotator/→replay_server/)
- Leave ACTIVE only if the files are genuinely expected to be created later.

{% for o in entries %}
- **{{ o.name }}**{% if o.id %} ({{ o.id }}){% endif %}: {{ o.paths | join(', ') }}
{% endfor %}

({{ entry_count }} orphaned concepts to resolve)

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

**Goal: get orphan count to 0.** Every entry must be resolved.
{% elif drift_type == "contested_review" %}
# Contested Claims Review (Chunk {{ chunk_id }})

This is a dedicated review round for long-standing contested claims.
These claims have been contested for longer than the review threshold.

For each claim below:
- **Resolve** if evidence now supports one position. Update status to believed or refuted.
- **Escalate** if resolution requires data not in the living docs. Add Agent guidance.
- **Keep contested** only if genuinely unresolved with active investigation.

{% for e in entries %}
- **{{ e.name }}**{% if e.id %} ({{ e.id }}){% endif %}: {{ e.days_old }} days unresolved (since {{ e.last_date }})
{% endfor %}

({{ entry_count }} contested claims to review)
{% elif drift_type == "stale_unverified" %}
# Stale Unverified Claims Review (Chunk {{ chunk_id }})

This is a dedicated review round for stale unverified claims.
These claims have remained unverified beyond the staleness threshold.

For each claim below:
- **Verify** if evidence now exists. Update status to believed with evidence.
- **Refute** if evidence contradicts the claim. Move to graveyard.
- **Flag** if verification requires external action. Add Agent guidance with specific steps.

{% for e in entries %}
- **{{ e.name }}**{% if e.id %} ({{ e.id }}){% endif %}: {{ e.days_old }} days unverified (since {{ e.last_date }})
{% endfor %}

({{ entry_count }} stale unverified claims to review)
{% elif drift_type == "workflow_synthesis" %}
# Workflow Synthesis Round (Chunk {{ chunk_id }})

This is a dedicated round to consolidate repeated workflow patterns.
The following CURRENT workflows may overlap or duplicate each other.

For each cluster of related workflows:
- **Merge** if they describe the same process. Mark extras as MERGED → W{target}.
- **Distinguish** if they are genuinely different. Clarify Context: fields.
- **Supersede** if one replaces another. Mark old as SUPERSEDED → W{new}.

{% for w in entries %}
- **{{ w.name }}**{% if w.id %} ({{ w.id }}){% endif %}
{% endfor %}

({{ entry_count }} workflow entries to review for synthesis)
{% endif %}

---

## Style

- Use Edit for surgical updates. Do NOT reproduce entire documents.
- Be succinct. DEAD/refuted entries: 1-2 sentences max.
- When moving entries to graveyard, append the full content there, then replace
  the living doc entry with a STUB (heading + arrow pointer only).

## After All Edits: Lint Check (Required)

Run the linter after completing all edits:
```
engram lint --project-root <project_root>
```
Fix every violation reported. Re-run until lint passes with 0 violations.
Do not stop until lint is clean.
