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

{% if pre_assigned_ids %}
### Pre-assigned IDs for this chunk

Use ONLY these IDs for new entries. Do NOT invent your own.

{% for cat, ids in pre_assigned_ids.items() %}
- {{ cat }}: {{ ids | join(', ') }}
{% endfor %}
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
    [Optional inline **History:** is allowed, but prefer external per-ID history file.]

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
- When prompts contradict docs/issues, the prompt is authoritative

## Style

- **Be succinct.** High information density. No filler.
- Timeline: short factual entries. What happened, why, what resulted.
- Concept registry: structured fields only. 5 lines ideal, 10 max.
- Epistemic state: 1-2 sentence position. History as bullet list. 1-sentence agent guidance.
- Workflow registry: structured fields only. Context + trigger/method.
- DEAD/refuted entries: 1-2 sentences max. Key lesson + replacement.
- **Budget matters.** Every line stays in context for future chunks. Be ruthless about cutting words.
- For epistemic entries, keep the main file concise. Store detailed append-only history in inferred files:
  `{{ epistemic_history_dir }}/E{NNN}.md` (derive from entry ID; do NOT add a `History file:` field).

## Important

- Use Edit for surgical updates. Do NOT reproduce entire documents.
- Only edit sections affected by new content.
- Capture cross-concept relationships using stable IDs (C###, E###, W###).
- Architect review comments on merged PRs = debt.
- When two artifacts make contradictory claims, that's an epistemic entry.
- Do NOT add entries about the fold process itself.
- When a concept's source files are deleted, mark it DEAD.
- When an ORPHANED CONCEPTS section is present, triage each one.
