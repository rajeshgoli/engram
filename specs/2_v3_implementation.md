# Epic: v3 Implementation (#2)

**Status:** Spec — in revision (review round 1)
**Date:** 2026-02-15
**Design doc:** `engram_idea.md` (#1)

---

## Scope

Implement engram v3 from the design in `engram_idea.md`. Port reusable code from `fractal-market-simulator/scripts/knowledge_fold.py` (the v2 script, on the `dev` branch — 992 lines). The `knowledge-fold` branch has an earlier 623-line snapshot; all references below are to `dev`. Build the new pieces: stable IDs, 4th doc, graveyard, schema linter, server daemon, CLI.

## Existing Code Inventory

The v2 script (992 lines, `dev` branch) breaks into three categories:

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
| `PROJECT_PATHS` filter | Replaced by config `sessions.project_match` |
| All hardcoded paths (`WORKTREE_PATH`, `LIVING_DOCS`, etc.) | Replaced by config |

Note: `HISTORY_FILE` / prompt session parsing (v2 lines 432-497) was previously listed as dead. **Restored to port scope** — user prompts are the most information-dense source of epistemic state (decisions, corrections, rationale). Ported as a pluggable session adapter in #4 with Claude Code as built-in format.

---

## Package Structure

```
engram/
├── pyproject.toml
├── engram/
│   ├── __init__.py
│   ├── cli.py              # Click CLI: engram init, seed, status, run
│   ├── config.py            # Load + validate .engram/config.yaml
│   ├── parse.py             # Shared markdown parsing (_parse_sections, heading regex)
│   ├── server/
│   │   ├── __init__.py
│   │   ├── watcher.py       # File/git event watching
│   │   ├── buffer.py        # Context buffer accumulation + budget + drift detection
│   │   ├── db.py            # SQLite state management (engram.db)
│   │   └── dispatcher.py    # Dispatch to fold agent + validate result
│   ├── fold/
│   │   ├── __init__.py
│   │   ├── queue.py         # Chronological queue building (from v2 build_queue)
│   │   ├── chunker.py       # Chunk assembly with drift priority (from v2 next_chunk)
│   │   ├── prompt.py        # Prompt template rendering (Jinja2)
│   │   ├── ids.py           # Stable ID allocation (monotonic counter, SQLite)
│   │   ├── sources.py       # Issue pulling, git dates, frontmatter parsing (from v2)
│   │   └── sessions.py      # Session history adapters (claude-code, codex)
│   ├── linter/
│   │   ├── __init__.py
│   │   ├── schema.py        # FULL/STUB heading validation per doc type
│   │   ├── refs.py          # Cross-reference resolution (C###/E###/W###)
│   │   └── guards.py        # Diff size guard, missing section detection, ID compliance
│   ├── compact/
│   │   ├── __init__.py
│   │   ├── graveyard.py     # Move DEAD/EVOLVED/refuted entries to graveyard
│   │   └── timeline.py      # Timeline phase collapse
│   ├── bootstrap/
│   │   ├── __init__.py
│   │   ├── seed.py          # Snapshot-based initial living docs
│   │   └── fold.py          # Historical fold from start date
│   └── templates/
│       ├── fold_prompt.md   # Fold agent system instructions (Jinja2)
│       ├── triage_prompt.md # Orphan triage prompt
│       └── seed_prompt.md   # Bootstrap seed prompt
├── tests/
│   ├── test_config.py
│   ├── test_parse.py
│   ├── test_queue.py
│   ├── test_chunker.py
│   ├── test_ids.py
│   ├── test_linter.py
│   ├── test_graveyard.py
│   ├── test_sources.py
│   ├── test_sessions.py
│   └── test_migrate.py
└── examples/
    └── config.yaml          # Example project config
```

---

## Sub-tickets

### #3 — Project scaffolding + config system
**Agent:** Engineer (single ticket)

Set up the Python package:
- `pyproject.toml` with click, pyyaml, watchdog, jinja2, filelock dependencies
- `engram/config.py`: load `.engram/config.yaml`, validate required fields, provide defaults. Sessions config includes `format` (built-in: `claude-code`, `codex`), `path`, and `project_match` fields.
- `engram/cli.py`: skeleton with `engram init` (creates `.engram/` dir + config template + empty living docs)
- `engram/parse.py`: shared markdown parsing — `_parse_sections()` (port from v2 lines 583-614) and heading regex utilities. This is the foundation module used by both linter (#6) and compaction (#7).
- Example config at `examples/config.yaml`
- Tests for config loading + validation + shared parser

**Port from v2:** Config constants (lines 28-82) become config.yaml fields. `_parse_sections()` (lines 583-614) becomes shared `engram/parse.py`.

**Acceptance:** `engram init` creates a working `.engram/config.yaml` and 4 empty living docs + 2 graveyard files with correct schema headers. `python -m pytest tests/test_config.py tests/test_parse.py` passes.

---

### #4 — Source ingestion + queue building
**Agent:** Engineer (single ticket)

Port the artifact ingestion pipeline:
- `engram/fold/sources.py`: `pull_issues()`, `_render_issue_markdown()`, `_get_doc_git_dates()`, `_parse_frontmatter_date()`, `_extract_issue_number()`, `_parse_date()`, `_git_diff_summary()` (new — v2 has this at lines 526-580 on `dev`)
- `engram/fold/sessions.py`: pluggable session adapter interface. Built-in adapters:
  - `claude-code`: parse `~/.claude/history.jsonl`, group by session, filter by `project_match` config, render as markdown (port from v2 lines 432-497). User prompts are the most information-dense source — they carry decisions, corrections, and rationale that no other artifact captures.
  - `codex`: planned, not in this epic (stub adapter with format TBD)
- `engram/fold/queue.py`: `build_queue()` — parameterized by config, outputs JSONL. Includes session entries alongside docs and issues.
- All paths from config, no hardcoded values
- CLI command: `engram build-queue`

**Port from v2 (`dev` branch):** `pull_issues` (lines 239-263), `build_queue` (lines 332-523), date/git helper functions (lines 266-329), `_git_diff_summary` (lines 526-580), session parsing (lines 432-497) parameterized via config `sessions.project_match` (replaces hardcoded `PROJECT_PATHS` filter at line 68).

**Acceptance:** `engram build-queue` on a test repo produces correct JSONL including session entries. Claude Code history adapter correctly filters by project, groups by session, renders prompts as markdown. `python -m pytest tests/test_queue.py tests/test_sources.py tests/test_sessions.py` passes.

---

### #5 — Stable ID allocation
**Agent:** Engineer (single ticket)

New code — no v2 equivalent:
- `engram/fold/ids.py`: monotonic counter backed by SQLite (`id_counters` table in `.engram/engram.db`). Atomic read/reserve/write via SQL transactions. Uses `server/db.py` from #9 for database access (or a shared `db.py` if #5 lands before #9 — interface is just `get_and_increment(category, count)`).
- Pre-scan chunk items to estimate new entity count, reserve ID ranges
- Include pre-assigned IDs in chunk metadata
- Counter categories: C (concepts), E (epistemic), W (workflows)
- IDs never reused, even after deletion

**Acceptance:** Concurrent-safe ID allocation (SQLite transactions). IDs are disjoint across chunks by construction. `python -m pytest tests/test_ids.py` passes.

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
epistemic_state:  FULL (believed|contested|unverified) requires Evidence: or History:. STUB (refuted) → pointer only.
workflow_registry: FULL (CURRENT) requires Context: + Trigger:/Current method:. STUB (SUPERSEDED|MERGED) → pointer only.
```

**Acceptance:** Linter correctly validates example docs from engram_idea.md. Catches: missing Code: on ACTIVE concept, missing Evidence:/History: on non-refuted epistemic entry, duplicate IDs, unresolved cross-references, oversized diffs, output IDs not matching pre-assigned IDs from chunk input. `python -m pytest tests/test_linter.py` passes.

---

### #7 — Graveyard compaction
**Agent:** Engineer (single ticket)

Rework of v2's `_compact_doc()` (lines 666-718) + `_find_orphaned_concepts()` (lines 628-663):
- `engram/compact/graveyard.py`: when entry flips DEAD/EVOLVED/refuted, move full entry to graveyard file, leave STUB in living doc. Append-only writes to graveyard. Correction block mechanism for reclassifications. Uses shared `engram/parse.py` from #3.
- `engram/compact/timeline.py`: when timeline.md exceeds threshold, collapse phases >6 months to single-paragraph summaries.
- Port `_find_orphaned_concepts()` (lines 628-663) parameterized by config.

**Acceptance:** Graveyard move produces correct STUB in living doc + full entry in graveyard. Timeline compaction preserves ID references. Correction blocks append correctly. `python -m pytest tests/test_graveyard.py` passes.

---

### #8 — Chunk assembly + prompt rendering
**Agent:** Engineer (single ticket)

Rework of v2's `next_chunk()` (lines 721-931) + `_write_agent_prompt()` (lines 933-975):
- `engram/fold/chunker.py`: build chunks with drift-priority scheduling. Dynamic budget: `context_limit - living_docs - overhead`, capped at max_chunk_chars. Includes pre-assigned IDs from #5.
- **Drift priority implementation** — all 4 threshold types from design (engram_idea.md lines 85-90):
  - Orphaned concepts (> `orphan_triage` threshold) → concept triage chunk
  - Contested claims unresolved > `contested_review_days` → resolution review chunk
  - Stale unverified claims > `stale_unverified_days` → evidence review chunk
  - Workflow repetitions > `workflow_repetition` threshold → workflow synthesis chunk
- `engram/fold/prompt.py`: Jinja2 templates for fold prompt, triage prompt, seed prompt. Templates in `engram/templates/`.
- Rework `SYSTEM_INSTRUCTIONS` (v2 lines 87-201) for v3: 4 docs, stable IDs, FULL/STUB forms, graveyard move instructions.
- CLI command: `engram next-chunk`

**Dependencies:** #3 (config + shared parser), #4 (queue), #5 (IDs)

**Acceptance:** `engram next-chunk` produces correct `chunk_NNN_input.md` + `chunk_NNN_prompt.txt`. Each of the 4 drift threshold types triggers priority scheduling when exceeded. Budget math is correct. `python -m pytest tests/test_chunker.py` passes.

---

### #9 — Server daemon (watch → accumulate → dispatch → validate)
**Agent:** Engineer (single ticket)

New code — the core v3 addition:
- `engram/server/watcher.py`: filesystem events via `watchdog` on configured source dirs. Git polling (`git log --since`) on configurable interval (default 60s) for commit/push detection. Session history polling (mtime check on `sessions.path`, e.g., `~/.claude/history.jsonl`) on same interval — new entries filtered by `project_match` and added to buffer.
- `engram/server/buffer.py`: accumulate changed items into context buffer, compute budget in real-time, trigger dispatch when full or drift threshold hit. Tracks age-based drift metrics (contested claim age, stale unverified age, workflow repetition count) to feed into #8 chunker's priority scheduling.
- `engram/server/dispatcher.py`: serial dispatch — one chunk at a time. Shell out to fold agent CLI (configurable: `claude`, `codex`, etc.), capture result, run linter, auto-retry with correction prompt on failure (max 2), regenerate L0 briefing after success.
- `engram/server/db.py`: SQLite state management (`.engram/engram.db`). Four tables: `buffer_items`, `dispatches` (lifecycle: building → dispatched → validated → committed), `id_counters`, `server_state`. Atomic transactions for buffer consumption + dispatch recording. Crash recovery on startup: check `dispatches` for non-terminal rows and resume.
- CLI commands: `engram run` (foreground process — user's process manager handles supervision), `engram status` (show buffer fill, last dispatch, pending items, dispatch history)

**Dependencies:** #3 (config), #5 (IDs), #6 (linter), #8 (chunker)

**Acceptance:** Server detects file changes, accumulates buffer, dispatches when full. Session history polling detects new prompts and adds them to buffer. Linter rejection triggers auto-retry. L0 briefing regenerated after successful dispatch. Crash recovery: kill server mid-dispatch, restart, server resumes correctly. `engram status` shows accurate state from SQLite. Manual tests: (1) create a file in watched dir → server dispatches within configured interval; (2) add a prompt to history file → prompt appears in next buffer/dispatch.

---

### #10 — Bootstrap: seed + historical fold
**Agent:** Engineer (single ticket)

New code — three bootstrap paths (see engram_idea.md Bootstrap section):
- `engram/bootstrap/seed.py`: read repo at a point-in-time snapshot, produce initial 4 living docs + 2 graveyard files via single agent dispatch. Uses `git worktree add` for historical snapshots (ephemeral, never touches user's working tree). Seed prompt template.
- `engram/bootstrap/fold.py`: chronological fold from a start date to today — reuses queue builder (#4) + chunker (#8) with date filter.
- CLI: `engram seed` (Path B: seed from today), `engram seed --from-date YYYY-MM-DD` (Path A: checkout snapshot at date, seed, fold forward to today)
- `engram fold --from YYYY-MM-DD` — standalone forward fold without re-seeding. Processes queue from a date to today against existing living docs. Used by Path C after migration, and by Path A internally after seeding.
- Path C (adopt existing v2 docs) is handled by #11 migration + `engram fold --from`.

**Dependencies:** #3 (config), #4 (queue), #8 (chunker)

**Acceptance:** `engram seed` on a test repo produces initial living docs with correct schema. `engram seed --from-date 2026-01-01` checks out repo at that date, seeds from snapshot, then folds forward through all artifacts from Jan 1 to today. Git worktree is cleaned up after seed completes.

---

### #11 — Stable ID migration for existing v2 docs (Bootstrap Path C)
**Agent:** Engineer (single ticket)

New code — one-time migration tool for projects with pre-existing v2 living docs (e.g., fractal-market-simulator's 17 chunks):
- CLI command: `engram migrate [--fold-from YYYY-MM-DD]` — upgrades v2 living docs to v3 format
- **ID backfill:** scan existing entries, assign C###/E###/W### IDs in document order (deterministic, stable across runs)
- **4th doc extraction:** identify workflow-like entries in existing docs, extract to new `workflow_registry.md` with W### IDs
- **Graveyard bootstrapping:** move existing DEAD/refuted entries to graveyard files, leave STUBs in living docs
- **Cross-reference rewrite:** replace name-based references with stable ID references
- **Counter initialization:** set `id_counters` in SQLite from max assigned IDs so subsequent allocations don't collide
- **Fold continuation:** if `--fold-from` provided, set the marker date in SQLite (e.g., `--fold-from 2026-01-01` for fractal where v2 stopped at Jan 1). User then runs `engram fold --from 2026-01-01` (#10) to process the gap, or `engram run` picks up from the marker for future changes.
- Validation pass: run linter (#6) after migration to confirm all entries and refs are valid

**Dependencies:** #3 (config), #5 (IDs), #6 (linter)

**Acceptance:** `engram migrate` on v2 living docs produces valid v3 docs: stable IDs on all entries, workflow_registry.md created, graveyard files populated with DEAD/refuted entries, cross-references resolve, counter initialized. Idempotent — running twice produces the same result. `python -m pytest tests/test_migrate.py` passes.

---

## Dependency Graph

```
#3 Scaffolding + config ──┬──→ #4 Sources + queue ──→ #8 Chunker + prompts ──→ #9 Server
                          ├──→ #5 Stable IDs ────────→ #8                    → #9
                          │                    ├──→ #11 Migration (+ #6)
                          ├──→ #6 Linter ──────┤────────────────────────────→ #9
                          └──→ #7 Graveyard
                                                      #8 ──→ #10 Bootstrap
```

**Parallelizable after #3:**
- #4, #5, #6, #7 can run in parallel (no cross-dependencies)
- #8 depends on #3 + #4 + #5
- #9 depends on #5 + #6 + #8
- #10 depends on #3 + #4 + #8
- #11 depends on #3 + #5 + #6

**Critical path:** #3 → #4 → #8 → #9

---

## Execution Plan

**Wave 1:** #3 scaffolding
**Wave 2 (parallel after #3):** #4 sources, #5 IDs, #6 linter, #7 graveyard
**Wave 3 (parallel after wave 2):** #8 chunker + prompts, #11 migration
**Wave 4 (parallel after #8):** #9 server, #10 bootstrap

Estimated: 4 waves, 9 tickets. Each ticket is one-agent-sized (completable in a single context window).
