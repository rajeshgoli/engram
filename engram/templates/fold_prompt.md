# Instructions

You are updating 4 knowledge documents based on new project artifacts.

Read the new content below, then edit the 4 living docs using your file editing tools:

- **{{ doc_paths.timeline }}** — Chronological narrative. Write in phases/events.
- **{{ doc_paths.concepts }}** — Concept registry keyed by stable ID (C###).
- **{{ doc_paths.epistemic }}** — Epistemic state keyed by stable ID (E###).
- **{{ doc_paths.workflows }}** — Workflow registry keyed by stable ID (W###).

## Stable IDs

Every entry uses a permanent stable ID: C### for concepts, E### for claims, W### for workflows.
IDs survive renames, refactoring, and evolution. Never reuse an ID.

### Pre-assigned IDs for this chunk

Use ONLY these IDs for new entries. Do NOT invent your own.
If no IDs are listed, do NOT create new entries/IDs in this chunk.

{% if pre_assigned_ids %}
{% for cat, ids in pre_assigned_ids.items() %}
- {{ cat }}: {{ ids | join(', ') }}
{% endfor %}
{% else %}
- (none)
{% endif %}

## Entry Formats

### FULL form (ACTIVE / CURRENT / believed / contested / unverified)

All required fields must be present.

**Concept registry:**

    ## C{NNN}: {name} (ACTIVE[ — {MODIFIER}])
    - **Code:** relevant source files
    - **Issues:** issue numbers
    - **Rationale:** documented | empty | questioned
    - **Debt:** known incompleteness
    - **Relationships:** connections (C###, E###, W###)

**Epistemic state:**

    ## E{NNN}: {name} (believed|contested|unverified)
    **Current position:** 1-2 sentences.
    **Agent guidance:** 1 sentence only.
    [Optional inline **History:** is allowed, but prefer external per-ID files.]

**Workflow registry:**

    ## W{NNN}: {name} (CURRENT[ — {MODIFIER}])
    - **Context:** when/why this workflow applies
    - **Trigger:** what initiates it (or **Current method:** how it's done)

### STUB form (DEAD / EVOLVED / SUPERSEDED / MERGED / refuted)

One-liner with graveyard pointer. No field requirements.

    ## C042: proximity_pruning (DEAD) → {{ doc_paths.concept_graveyard | basename }}#C042
    ## E007: phantom_wr (refuted) → {{ doc_paths.epistemic_graveyard | basename }}#E007
    ## W003: manual_deploy (SUPERSEDED) → W008

## Graveyard Moves

When a concept flips DEAD or EVOLVED, or a claim flips refuted:
1. **Append** the full entry to the graveyard file:
   - Concepts → {{ doc_paths.concept_graveyard }}
   - Claims → {{ doc_paths.epistemic_graveyard }}
2. **Replace** the living doc entry with a STUB (one-liner + pointer).

Graveyard files are append-only. Never edit existing graveyard entries.

## How to Handle Each Item Type

**INITIAL items** (first time seeing this artifact):
- Extract concepts, claims, timeline events, and workflow patterns
- Add new entries using pre-assigned IDs
- If content references things not yet in the living docs, add provisionally

**REVISIT items** (updated since first processed):
- Check what exists from the initial pass
- Update entries based on changes
- Pay attention to epistemic shifts — revisits often mean claims were tested

**USER PROMPTS** (project owner's direct inputs):
- Most information-dense items. A single sentence often encodes a major decision.
- They reveal: intent, corrections, decisions, priorities, dead ends, rationale
- Read as conversation threads — sequence within a session tells a story
- Prompt lines like `[Pasted text #N +M lines]` are placeholder markers for omitted pasted blocks.
  Treat them as "user pasted external context" signals, not literal project facts.
- When prompts contradict docs/issues, the prompt is authoritative

## Style

- **Be succinct.** High information density. No filler.
- Timeline: short factual entries. What happened, why, what resulted.
- Concept registry: structured fields only. 5 lines ideal, 10 max.
- Epistemic state: 1-2 sentence position. 1-sentence agent guidance.
- Workflow registry: structured fields only. Context + trigger/method.
- DEAD/refuted entries: 1-2 sentences max. Key lesson + replacement.
- **Budget matters.** Every line stays in context for future chunks. Be ruthless about cutting words.
- For epistemic entries, use inferred per-ID files:
  - Mutable current state (rewrite when E{NNN} changes): `{{ epistemic_current_dir }}/E{NNN}.md`
  - Append-only history log (append only): `{{ epistemic_history_dir }}/E{NNN}.md`
- Keep `{{ doc_paths.epistemic }}` concise. Put detailed, coherent per-claim state in the current file.

## Important

- Use Edit for surgical updates. Do NOT reproduce entire documents.
- Only edit sections affected by new content.
- Capture cross-concept relationships using stable IDs (C###, E###, W###).
{% if context_worktree_path %}
- If you inspect repo files, use ONLY `{{ context_worktree_path }}`{% if context_commit %} (commit `{{ context_commit[:12] }}`){% endif %}.
- Do NOT inspect source files from the project-root workspace for this chunk.
{% else %}
- For normal fold chunks, use ONLY this input file + the 4 living docs.
- Do NOT inspect source code, git history, or filesystem state to verify claims.
{% endif %}
- Architect review comments on merged PRs = debt.
- When two artifacts make contradictory claims, that's an epistemic entry.
- For each touched E{NNN}, keep exactly one coherent current position (no contradictory parallel states).
- Do NOT add entries about the fold process itself.
- If this input explicitly states a concept's source files were deleted, mark it DEAD.
- When an ORPHANED CONCEPTS section is present, triage each one.
- If content is ambiguous or contradictory and cannot be resolved from this input, record/maintain the uncertainty in epistemic state (e.g., contested) rather than self-verifying from repo code.
