# Epic: v3 Implementation (#2)

**Status:** Spec — ready for review
**Date:** 2026-02-15
**Design doc:** `engram_idea.md` (#1)

---

## Scope

Implement engram v3 from the design in `engram_idea.md`. Port reusable code from `fractal-market-simulator/scripts/knowledge_fold.py` (the v2 script). Build the new pieces: stable IDs, 4th doc, graveyard, schema linter, server daemon, CLI.

## Existing Code Inventory

The v2 script (~993 lines) breaks into three categories:

### Reusable (~60%, port with parameterization)

| Function | What it does | Port effort |
|----------|-------------|-------------|
| `_render_issue_markdown()` | Issue JSON → markdown | Trivial — already generic |
| `_get_doc_git_dates()` | Git first/last commit dates for a file | Parameterize `PROJECT_ROOT` |
| `_parse_frontmatter_date()` | Extract date from doc frontmatter | Parameterize `PROJECT_START` |
| `_extract_issue_number()` | Issue # from filename pattern | Trivial |
| `_parse_date()` | ISO date string parsing | Trivial — already generic |
| `build_queue()` | Build chronological queue with dual-pass | Parameterize all paths via config |
| `pull_issues()` | Pull GH issues to local JSON | Parameterize repo name |
| `_git_diff_summary()` | File creates/deletes/renames in date range | Parameterize `PROJECT_ROOT` |
| `_find_orphaned_concepts()` | Detect ACTIVE concepts with missing files | Parameterize root + file patterns |
| `_parse_sections()` | Parse markdown into H2 sections with status | Already generic |

### Needs rework (~30%)

| Function | Why |
|----------|-----|
| `_compact_doc()` | v3 uses graveyard move instead of in-place stripping |
| `next_chunk()` | Needs: 4 docs, stable ID pre-allocation, drift-priority scheduling, linter call, config paths |
| `_write_agent_prompt()` | Needs templating from config, 4 docs, stable ID instructions |
| `SYSTEM_INSTRUCTIONS` | v3 prompt: 4 docs, FULL/STUB forms, graveyard instructions |

### Dead (don't port)

| Item | Why |
|------|-----|
| `HISTORY_FILE` / prompt session parsing | Claude Code history.jsonl — project-specific source type |
| `PROJECT_PATHS` filter | Hardcoded project names |
| All hardcoded paths (`WORKTREE_PATH`, `LIVING_DOCS`, etc.) | Replaced by config |

---

## Package Structure

```
engram/
├── pyproject.toml
├── engram/
│   ├── __init__.py
│   ├── cli.py              # Click CLI: engram init, seed, status, run
│   ├── config.py            # Load + validate .engram/config.yaml
│   ├── server/
│   │   ├── __init__.py
│   │   ├── watcher.py       # File/git event watching
│   │   ├── buffer.py        # Context buffer accumulation + budget
│   │   └── dispatcher.py    # Dispatch to fold agent + validate result
│   ├── fold/
│   │   ├── __init__.py
│   │   ├── queue.py         # Chronological queue building (from v2 build_queue)
│   │   ├── chunker.py       # Chunk assembly with drift priority (from v2 next_chunk)
│   │   ├── prompt.py        # Prompt template rendering
│   │   ├── ids.py           # Stable ID allocation (monotonic counter)
│   │   └── sources.py       # Issue pulling, git dates, frontmatter parsing (from v2)
│   ├── linter/
│   │   ├── __init__.py
│   │   ├── schema.py        # FULL/STUB heading validation per doc type
│   │   ├── refs.py          # Cross-reference resolution (C###/E###/W###)
│   │   └── guards.py        # Diff size guard, missing section detection
│   ├── compact/
│   │   ├── __init__.py
│   │   ├── graveyard.py     # Move DEAD/EVOLVED/refuted entries to graveyard
│   │   └── timeline.py      # Timeline phase collapse
│   └── templates/
│       ├── fold_prompt.md   # Fold agent system instructions (Jinja2 or similar)
│       ├── triage_prompt.md # Orphan triage prompt
│       └── seed_prompt.md   # Bootstrap seed prompt
├── tests/
│   ├── test_config.py
│   ├── test_queue.py
│   ├── test_chunker.py
│   ├── test_ids.py
│   ├── test_linter.py
│   ├── test_graveyard.py
│   └── test_sources.py
└── examples/
    └── config.yaml          # Example project config
```

---

## Sub-tickets

### #3 — Project scaffolding + config system
**Agent:** Engineer (single ticket)

Set up the Python package:
- `pyproject.toml` with click, pyyaml, watchdog dependencies
- `engram/config.py`: load `.engram/config.yaml`, validate required fields, provide defaults
- `engram/cli.py`: skeleton with `engram init` (creates `.engram/` dir + config template + empty living docs)
- Example config at `examples/config.yaml`
- Tests for config loading + validation

**Port from v2:** Config constants (lines 28-82) become config.yaml fields.

**Acceptance:** `engram init` creates a working `.engram/config.yaml` and 4 empty living docs + 2 graveyard files with correct schema headers. `python -m pytest tests/test_config.py` passes.

---

### #4 — Source ingestion + queue building
**Agent:** Engineer (single ticket)

Port the artifact ingestion pipeline:
- `engram/fold/sources.py`: `pull_issues()`, `_render_issue_markdown()`, `_get_doc_git_dates()`, `_parse_frontmatter_date()`, `_extract_issue_number()`, `_parse_date()`, `_git_diff_summary()`
- `engram/fold/queue.py`: `build_queue()` — parameterized by config, outputs JSONL
- All paths from config, no hardcoded values
- CLI command: `engram build-queue`

**Port from v2:** `pull_issues` (239-263), `build_queue` (332-523), all date/git helper functions (204-329), `_git_diff_summary` (526-580). Drop the `HISTORY_FILE` / prompt session parsing (431-497) — that was fractal-specific.

**Acceptance:** `engram build-queue` on a test repo produces correct JSONL. `python -m pytest tests/test_queue.py tests/test_sources.py` passes.

---

### #5 — Stable ID allocation
**Agent:** Engineer (single ticket)

New code — no v2 equivalent:
- `engram/fold/ids.py`: monotonic counter file (`id_counters.json`), atomic read/reserve/write
- Pre-scan chunk items to estimate new entity count, reserve ID ranges
- Include pre-assigned IDs in chunk metadata
- Counter categories: C (concepts), E (epistemic), W (workflows)
- IDs never reused, even after deletion

**Acceptance:** Concurrent-safe ID allocation (file locking). IDs are disjoint across chunks by construction. `python -m pytest tests/test_ids.py` passes.

---

### #6 — Schema linter + invariant checks
**Agent:** Engineer (single ticket)

New code — no v2 equivalent:
- `engram/linter/schema.py`: validate FULL vs STUB entry forms per doc type, status-gated heading regex
- `engram/linter/refs.py`: cross-reference validation (every C###/E###/W### resolves)
- `engram/linter/guards.py`: diff size guard (>2x expected = flag), missing section detection
- Main entry: `lint_result = lint(living_docs, graveyard_docs, config)` returns pass/fail + violation list
- CLI command: `engram lint`

**Schema rules (from engram_idea.md):**

```
concept_registry: FULL (ACTIVE) requires Code:. STUB (DEAD|EVOLVED) → pointer only.
epistemic_state:  FULL (believed|contested|unverified) requires Evidence:. STUB (refuted) → pointer only.
workflow_registry: FULL (CURRENT) requires Context: + Trigger:/Current method:. STUB (SUPERSEDED|MERGED) → pointer only.
```

**Acceptance:** Linter correctly validates example docs from engram_idea.md. Catches: missing Code: on ACTIVE concept, duplicate IDs, unresolved cross-references, oversized diffs. `python -m pytest tests/test_linter.py` passes.

---

### #7 — Graveyard compaction
**Agent:** Engineer (single ticket)

Rework of v2's `_compact_doc()` (666-718) + `_find_orphaned_concepts()` (628-663):
- `engram/compact/graveyard.py`: when entry flips DEAD/EVOLVED/refuted, move full entry to graveyard file, leave STUB in living doc. Append-only writes to graveyard. Correction block mechanism for reclassifications.
- `engram/compact/timeline.py`: when timeline.md exceeds threshold, collapse phases >6 months to single-paragraph summaries.
- Port `_parse_sections()` (583-614) as shared utility — it's used by both linter and compaction.
- Port `_find_orphaned_concepts()` (628-663) parameterized by config.

**Acceptance:** Graveyard move produces correct STUB in living doc + full entry in graveyard. Timeline compaction preserves ID references. Correction blocks append correctly. `python -m pytest tests/test_graveyard.py` passes.

---

### #8 — Chunk assembly + prompt rendering
**Agent:** Engineer (single ticket)

Rework of v2's `next_chunk()` (721-931) + `_write_agent_prompt()` (933-975):
- `engram/fold/chunker.py`: build chunks with drift-priority scheduling (orphans/contradictions first, then chronological). Dynamic budget: `context_limit - living_docs - overhead`, capped at max_chunk_chars. Includes pre-assigned IDs from #5.
- `engram/fold/prompt.py`: Jinja2 templates for fold prompt, triage prompt, seed prompt. Templates in `engram/templates/`.
- Rework `SYSTEM_INSTRUCTIONS` (v2 lines 87-201) for v3: 4 docs, stable IDs, FULL/STUB forms, graveyard move instructions.
- CLI command: `engram next-chunk`

**Dependencies:** #3 (config), #4 (queue), #5 (IDs)

**Acceptance:** `engram next-chunk` produces correct `chunk_NNN_input.md` + `chunk_NNN_prompt.txt`. Drift items are prioritized when thresholds exceeded. Budget math is correct. `python -m pytest tests/test_chunker.py` passes.

---

### #9 — Server daemon (watch → accumulate → dispatch → validate)
**Agent:** Engineer (single ticket)

New code — the core v3 addition:
- `engram/server/watcher.py`: watch for git events, file changes in configured source dirs (using `watchdog` or polling)
- `engram/server/buffer.py`: accumulate changed items into context buffer, compute budget in real-time, trigger dispatch when full or drift threshold hit
- `engram/server/dispatcher.py`: dispatch chunk to fold agent (shell out to `claude` / `codex` CLI), capture result, run linter, auto-retry on failure (max 2), regenerate L0 briefing after success
- CLI command: `engram run` (foreground daemon), `engram status` (show buffer fill, last dispatch, pending items)

**Dependencies:** #3 (config), #5 (IDs), #6 (linter), #8 (chunker)

**Acceptance:** Server detects file changes, accumulates buffer, dispatches when full. Linter rejection triggers auto-retry. L0 briefing regenerated after successful dispatch. Manual test: create a file in watched dir → server dispatches within configured interval.

---

### #10 — Bootstrap: seed + historical fold
**Agent:** Engineer (single ticket)

New code:
- `engram/bootstrap/seed.py`: read repo at current state (code structure, docs, recent issues), produce initial living docs via single agent dispatch. Seed prompt template.
- `engram/bootstrap/fold.py`: historical fold from a start date — reuses queue builder (#4) + chunker (#8) with date filter. CLI: `engram seed [--from-date YYYY-MM-DD]`
- If `--from-date` provided: seed + fold forward from that date. If omitted: seed only (snapshot).

**Dependencies:** #3 (config), #4 (queue), #8 (chunker)

**Acceptance:** `engram seed` on a test repo produces initial living docs with correct schema. `engram seed --from-date 2026-01-01` processes only artifacts after that date.

---

## Dependency Graph

```
#3 Scaffolding + config ──┬──→ #4 Sources + queue ──→ #8 Chunker + prompts ──→ #9 Server
                          ├──→ #5 Stable IDs ────────→ #8                    → #9
                          ├──→ #6 Linter ────────────────────────────────────→ #9
                          └──→ #7 Graveyard
                                                      #8 ──→ #10 Bootstrap
```

**Parallelizable after #3:**
- #4, #5, #6, #7 can run in parallel (no cross-dependencies)
- #8 depends on #4 + #5
- #9 depends on #5 + #6 + #8
- #10 depends on #4 + #8

**Critical path:** #3 → #4 → #8 → #9

---

## Execution Plan

**Wave 1 (parallel):** #3 scaffolding
**Wave 2 (parallel after #3):** #4 sources, #5 IDs, #6 linter, #7 graveyard
**Wave 3 (after #4, #5):** #8 chunker + prompts
**Wave 4 (parallel after #8):** #9 server, #10 bootstrap

Estimated: 4 waves, 8 tickets. Each ticket is one-agent-sized (completable in a single context window).
