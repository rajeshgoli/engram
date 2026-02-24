# Bootstrap Seed Instructions

You are initializing 4 knowledge documents for a new project.

Read all the content below, then populate the living docs with initial entries:

- **{{ doc_paths.timeline }}** — Start the chronological narrative from the earliest events.
- **{{ doc_paths.concepts }}** — Register all code concepts found with stable IDs (C001, C002, ...).
- **{{ doc_paths.epistemic }}** — Capture all claims and beliefs with stable IDs (E001, E002, ...).
- **{{ doc_paths.workflows }}** — Document all process patterns with stable IDs (W001, W002, ...).

{% if pre_assigned_ids %}
### Pre-assigned IDs

Use ONLY these IDs for new entries. Do NOT invent your own.

{% for cat, ids in pre_assigned_ids.items() %}
- {{ cat }}: {{ ids | join(', ') }}
{% endfor %}
{% endif %}

## Entry Formats

**Concepts (FULL form):**

    ## C{NNN}: {name} (ACTIVE)
    - **Code:** source files
    - **Issues:** related issue numbers
    - **Relationships:** connections (C###, E###, W###)

**Epistemic claims (FULL form):**

    ## E{NNN}: {name} (believed|unverified)
    **Current position:** 1-2 sentences.
    **Agent guidance:** 1 sentence.

**Workflows (FULL form):**

    ## W{NNN}: {name} (CURRENT)
    - **Context:** when/why
    - **Trigger:** what initiates it

## Style

- Be succinct. 5 lines per entry ideal, 10 max.
- Focus on what exists now, not historical evolution (that comes in subsequent chunks).
- Capture relationships between concepts using stable IDs.
- Mark anything uncertain as (unverified) — subsequent chunks will provide evidence.
- For each E{NNN}, maintain inferred per-ID files:
  - mutable current state: `{{ doc_paths.epistemic | replace('.md', '') }}/current/E{NNN}.md`
  - append-only history: `{{ doc_paths.epistemic | replace('.md', '') }}/history/E{NNN}.md`
- For concepts and workflows, keep mutable current-state details in:
  - `{{ concept_current_dir }}/C{NNN}.md`
  - `{{ workflow_current_dir }}/W{NNN}.md`
