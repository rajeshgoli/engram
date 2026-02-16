# Engram: Persistent Memory for AI Agents (#1)

**Status:** v3 design — under review
**Date:** 2026-02-15 (v3), 2026-02-10 (v2)

---

## Problem

Agents start fresh every session with no way to:

1. **Know what's dead.** Proximity pruning, LegDetector, 74% WR — these appear in CLAUDE.md, archive filenames, code descriptions. Agents pick them up and use them confidently. There's no signal that they're dead.

2. **Detect contradictions.** #1343 says 74% WR. #1404 says 48.9%. #1418 says 69.7% at tick level. Three docs, three claims about the same thing. An agent reads one and builds on it without knowing the others exist.

3. **Find non-obvious relationships.** #1418 is a degenerate case of #1302 Layer 1 — single `check_level_hits()` operation called from a different event loop. Keyword search won't surface this. The overlap is semantic.

4. **Understand epistemic state.** Some claims are believed, some refuted, some contested pending evidence. Without knowing which is which, agents build features on false premises.

5. **Trace workflow evolution.** Investigation methodology, review protocols, agent coordination patterns — all evolved through trial and error. No agent can see that evolution or learn from it.

Currently, the project owner re-explains context every session. Knowledge evaporates when sessions end.

---

## Solution Overview

A **knowledge server** that continuously maintains project knowledge by watching for changes and dispatching fold agents when new context accumulates. The server is project-agnostic (standalone public repo). Each project stores its own knowledge docs.

### Core Ideas

1. **4 living documents** capture everything worth knowing: what happened (timeline), what exists (concepts), what we believe (claims), and how we work (workflows).
2. **Stable IDs** are primary keys. File paths and function names are secondary pointers. A concept can move files ten times without becoming a new concept.
3. **Liveness and validity are separate ontologies.** Concepts are alive/dead/evolved (derived from repo state). Claims are believed/refuted/contested (derived from evidence). Agents cannot blur these categories.
4. **Drift drives scheduling.** The only things worth spending tokens on are new knowledge and resolving drift. Orphans, contradictions, and stale claims get priority over chronological processing.
5. **Compaction is lossless.** Living docs stay concise. Verbose historical detail moves to append-only graveyard files, queryable by ID on demand.
6. **Continuous operation.** The server watches repo events (commits, issues, doc changes), accumulates context, and auto-dispatches when the buffer fills. Knowledge gap is bounded by dispatch latency, not human initiative.

---

## Architecture

### The Knowledge Server

A lightweight daemon that watches for project changes and maintains living knowledge docs.

```
Events (git push, issue filed, PR merged, doc saved)
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Knowledge Server                                │
│                                                   │
│  1. Watch: git events, issues, doc changes        │
│  2. Accumulate: context buffer (items + drift)    │
│  3. Measure: living doc sizes (wc, cheap)         │
│  4. Budget: remaining = limit - docs - overhead   │
│  5. Dispatch: buffer full OR drift threshold hit  │
│  6. Validate: schema linter + invariant checks    │
│  7. Retry: auto (max 2), then flag for human      │
│  8. Compress: regenerate L0 briefing after each   │
└─────────────────────────────────────────────────┘
```

**Why a server, not a script?** A script requires someone to run it. The moment development gets busy, knowledge drifts. The server makes maintenance ambient — the same events that cause drift trigger the updates that resolve it. Cost is proportional to change rate: $0/month if quiet, $1-2/month during active development.

**Buffer construction is pure CPU.** No LLM calls until dispatch. Watching for changes, computing sizes, maintaining a queue — milliseconds per event.

**Dispatch frequency is self-regulating.** Busy days fill the buffer faster, producing more dispatches. Quiet periods accumulate slowly. The budget-based cutting handles this naturally.

### Scheduling Priority

The server fills each chunk using a priority system, not purely chronological order:

| Priority | Type | Trigger |
|----------|------|---------|
| 1 | Drift triage | Orphan count > N, contradictions > M, stale claims > K days |
| 2 | New knowledge | Chronological order (default) |
| 3 | Workflow changes | Detected methodology/process shifts |

During bootstrap, chronological order dominates (everything is new). In steady state, drift resolution takes priority because it's the only thing that keeps the docs accurate.

Generalized thresholds:

| Drift type | Threshold | Chunk type |
|------------|-----------|------------|
| Orphaned concepts | > 50 | Concept triage |
| Contested claims unresolved > 14 days | > 5 | Resolution review |
| Stale unverified claims > 30 days | > 10 | Evidence review |
| Workflow repetitions detected | > 3 instances | Workflow synthesis |

### Tiered Retrieval

```
L0:   CLAUDE.md briefing (~50-100 lines)
      Always loaded, every session. Auto-regenerated after each dispatch.
      "proximity pruning is dead, 74% WR is phantom,
       1418 tick-level edge is unverified pending broker data"

L1:   4 living docs (concise, growth-rate-bounded)
      Loaded during investigation. Stable-ID-keyed entries.

L1.5: 2 graveyard files (append-only)
      Loaded when L1 points to them. Full DEAD/refuted entries.

L2:   Individual working docs / archive
      Loaded when L1 or L1.5 points to them.
```

### Stable Identity System

**Concept IDs are primary keys. File paths are secondary pointers.**

```
C042: check_level_hits
  Code: SignalDetector.check_level_hits(), level_tracking.py
  ↑ primary (stable)     ↑ secondary (can change)
```

Why this matters: AI-assisted refactoring accelerates file path drift. The system's own success makes path-based identity unstable. With stable IDs, a concept can be refactored ten times without "dying" and being reborn. The fold agent's job becomes "update the pointer on C042" rather than "is this a death or an evolution?"

ID schemes:
- Concepts: `C001`, `C002`, ... (sequential, never reused)
- Claims: `E001`, `E002`, ... (sequential, never reused)
- Workflows: `W001`, `W002`, ... (sequential, never reused)
- Timeline: no IDs needed (narrative, not a lookup table)

Cross-references use IDs: "see C042", "contradicts E007", "supersedes W003". The schema linter validates that all referenced IDs resolve.

### ID Allocation

**The server is the single writer.** It owns a monotonic counter file (e.g., `local_data/id_counters.json`) with the next available ID per category:

```json
{"C": 89, "E": 34, "W": 15}
```

IDs are **pre-assigned in the chunk input**, not allocated by the fold agent. When the server builds a chunk, it scans the new items for entities that don't yet have IDs, reserves the next available IDs, and includes the assignments in the chunk instructions:

```markdown
## New ID assignments for this chunk
- C089: (new concept from issue #1520 — assign when you create the entry)
- E034: (new claim from doc 1520_analysis.md — assign when you create the entry)
```

The fold agent uses pre-assigned IDs; it never invents its own. This makes out-of-order content processing safe — two chunks can run in any order because their IDs are disjoint by construction. The counter file is updated atomically before dispatch.

### Ontology: Liveness vs Validity

Two fundamentally different categories with different lifecycles:

| Category | Lives in | Status values | Derived from | Cannot be |
|----------|----------|---------------|--------------|-----------|
| Concept (code construct) | concept_registry | ACTIVE / DEAD / EVOLVED | Repo state (partially automatable) | "refuted" or "believed" |
| Claim (epistemic assertion) | epistemic_state | believed / refuted / contested / unverified | Evidence chain (never automatable) | "alive" or "dead" |
| Debt (implementation gap) | concept_registry as modifier | HAS DEBT / cleared | Review artifacts | An independent entry |
| Workflow (process pattern) | workflow_registry | CURRENT / SUPERSEDED / MERGED | Process observation | "refuted" (processes aren't true/false) |

**Structural enforcement:** Concept entries MUST have `Code:` pointers. Epistemic entries MUST have `Evidence:` chains. If an entry has no code pointer, it's not a concept. If an entry has no evidence chain, it's not a claim. The schema linter rejects violations.

**The category error this prevents:** "74% win rate" is a claim (E-series), not a concept (C-series). The *concept* is `2x_pullback_signal` (C-series, the code that detects it). The *claim* is "2x_pullback has 74% WR" (E-series, refuted by evidence). Different docs, different lifecycles, different status values.

### Schema Linter + Invariant Checks

Run automatically after every agent dispatch. Violations trigger auto-retry with correction prompt (max 2 retries).

Entries have two forms, determined by status:

**FULL form** (ACTIVE, CURRENT, or any believed/contested/unverified claim):
All required fields for that doc type must be present.

**STUB form** (DEAD, EVOLVED, SUPERSEDED, MERGED, or refuted claim):
Heading + arrow pointer only. No field requirements. These are compacted
entries whose full content lives in graveyard files.

```
concept_registry.md:
  FULL: ## C{NNN}: {name} (ACTIVE[ — {MODIFIER}])
    MODIFIER (optional): HAS DEBT, RATIONALE QUESTIONED, etc.
    Required fields: Code:
    Optional fields: Issues:, Aliases:, Relationship:, Rationale:, Debt:
  STUB: ## C{NNN}: {name} (DEAD|EVOLVED) → {graveyard_target}
    No field requirements.
  - No duplicate concept IDs
  - All cross-references (C###, E###, W###) resolve

epistemic_state.md:
  FULL: ## E{NNN}: {name} (believed|contested|unverified)
    Required fields: Evidence: or History:
    Optional fields: Related concepts:, Agent guidance:
  STUB: ## E{NNN}: {name} (refuted) → {graveyard_target}
    No field requirements.

workflow_registry.md:
  FULL: ## W{NNN}: {name} (CURRENT[ — {MODIFIER}])
    MODIFIER (optional): HAS DEBT, etc.
    Required fields: Context:, and one of Trigger: or Current method:
  STUB: ## W{NNN}: {name} (SUPERSEDED|MERGED) → {target}
    No field requirements.

timeline.md:
  - Phases are chronologically ordered
  - Every phase has a date range

Cross-doc:
  - Every ID referenced in any doc exists in its home doc
  - No orphaned references
  - IDs in chunk output match pre-assigned IDs from chunk input
```

**Diff size guard:** If a chunk produces a diff larger than 2x expected growth rate, flag for review before accepting. Catches duplication, wholesale rewrites, or model confusion.

### Lossless Compaction (Graveyard)

When a concept flips DEAD or EVOLVED, or a claim flips refuted, the full entry moves to a graveyard file. The living doc keeps a one-liner plus pointer:

```markdown
## C042: proximity_pruning (DEAD) → concept_graveyard.md#C042
```

Two graveyard files:
- `concept_graveyard.md` — full DEAD/EVOLVED entries with lessons, residue, replacement info
- `epistemic_graveyard.md` — full refuted evidence chains with what refuted them

Both are append-only, keyed by ID, in the same directory as the living docs. Agents load them during investigation (L1.5), never during normal session init.

**Correction mechanism:** Graveyard entries are append-only — existing content is never edited. When a misclassification is discovered (e.g., C012 was marked DEAD but was actually EVOLVED), append a correction block:

```markdown
## C012 CORRECTION (2026-02-20)
Reclassified: DEAD → EVOLVED → C089
Original DEAD entry above is superseded. See C089 in concept_registry.md.
```

The latest block for any ID is authoritative. Original entry stays for audit trail.

**Growth model:**

| Doc type | Grows? | Growth rate | Bounded by |
|----------|--------|-------------|------------|
| Living docs (4) | Yes, growth-rate-bounded | ~30 chars/DEAD stub, ~200 chars/new ACTIVE entry | Only ACTIVE concepts, current claims, current workflows. DEAD/EVOLVED stubs are one-liners (~30 chars each). |
| Graveyard files (2) | Yes, monotonically | ~500 chars/entry | Append-only, never in normal context budget |
| Timeline | Yes, growth-rate-bounded | ~1-2K chars/phase | Older phases collapse to single-paragraph summaries when total exceeds threshold (see below) |
| L0 briefing | Bounded | N/A | Fixed ~50-100 lines |

**Timeline compaction:** When timeline.md exceeds a configurable size threshold (e.g., 50K chars), the server triggers a compaction pass on the oldest phases. Phases older than 6 months collapse from multi-paragraph narratives to single-paragraph summaries preserving key concept/claim IDs. Full narratives are preserved in git history. This bounds timeline growth to roughly: `(recent_phases × full_detail) + (old_phases × summary)`.

This is strictly better than git-history-based preservation. Git history works but isn't queryable by agents cheaply. A graveyard file keyed by ID is immediately readable.

---

## Bootstrap

### For New Projects: Snapshot + Incremental

Instead of processing all history from the first commit, drop an agent into the repo at a specific date and build understanding from there.

**Phase 1 — Seed:** An agent reads the repo as it exists today: code structure, existing docs, recent issues. Produces initial living docs — a snapshot understanding, like onboarding a new engineer.

**Phase 2 — Server starts:** The knowledge server begins watching for changes. From this point, every change is tracked incrementally.

**Phase 3 — Optional deep fold:** If historical context matters (e.g., understanding why certain designs exist), process artifacts back to a configurable start date. This is the historical fold — useful but not required.

| Approach | Cost | Lifecycle history | When to use |
|----------|------|-------------------|-------------|
| Snapshot only | ~$0.50-1 | None (current state only) | New projects, small repos |
| Snapshot + fold from date | ~$2-5 | Partial (from chosen date) | Projects with significant recent evolution |
| Full historical fold | ~$15-25 | Complete | Projects where design rationale matters deeply |

For fractal-market-simulator: the 17 chunks already processed (Oct–Jan 1) cover the early architecture and major pivots. Preserve those living docs as the seed, then let the server run forward from the current date.

### Dual-Pass (Bootstrap Only)

During bootstrap fold, docs appear in the queue at most twice: once at created date (INITIAL), once at last-modified date (REVISIT) — but only if dates are far enough apart to land in different chunks.

This is a **bootstrap policy**, not a permanent contract. Docs that evolve repeatedly after bootstrap are handled by the server's continuous monitoring, not by additional queue entries. The server sees the change event and includes the updated doc in the next buffer.

### The Fold Agent Contract

The fold agent is invoked per-chunk. It reads one input file and edits the living docs using native file tools. No custom tool schemas.

```
Input:  chunk_NNN_input.md (instructions + living docs snapshot + new items)
Output: surgical edits to 4 living docs + moves to graveyard files
```

The agent framework (Claude Code, Codex, etc.) handles file reading and editing. The server's only job is producing the input file and validating the result.

**Invocation:**
```bash
claude "Read local_data/chunks/chunk_042_input.md and update the 4 living docs
  and graveyard files based on the new content."
```

**Known failure mode:** Silent partial edits. The agent can edit the wrong section, truncate, or create subtle duplication. The schema linter catches structural violations. The diff size guard catches volumetric anomalies. Together they convert most silent failures into recoverable retries.

---

## Document Schemas

### Concept Registry (`concept_registry.md`)

Each entry keyed by stable ID:

```markdown
## C042: check_level_hits (ACTIVE)
- **Code:** `SignalDetector.check_level_hits()`, `level_tracking.py`
- **Issues:** #1302, #1418, #1340
- **Aliases:** check_level_hits, level_hit_checker
- **Relationship:** #1418 is degenerate case of #1302 Layer 1
- **Rationale:** documented — single entry point for level monitoring
- **Debt:** None

## C043: TradeLogPanel (ACTIVE — HAS DEBT)
- **Code:** `frontend/src/components/TradeLogPanel.tsx`
- **Issues:** #1263, #1265
- **Rationale:** documented — dedicated panel for trade display
- **Debt:** Bottom panel rendering not wired in Fractal.tsx (PR #1437)

## C044: SignalGate + SignalResolver (ACTIVE — RATIONALE QUESTIONED)
- **Code:** `swing_analysis/signal_gate.py`, `swing_analysis/signal_resolver.py`
- **Issues:** #1340, #1389
- **Rationale:** QUESTIONED — separation accepted for expediency, may be duplication

## C012: proximity_pruning (DEAD) → concept_graveyard.md#C012
```

**Fields:**
- `Code:` — file paths (secondary pointers, updated when code moves)
- `Issues:` — related issue numbers
- `Aliases:` — alternative names for the same concept
- `Relationship:` — semantic links to other concepts (by ID)
- `Rationale:` — documented / empty / questioned
- `Debt:` — None / description of known gap
- `Status:` — ACTIVE / DEAD / EVOLVED → C{other}

### Epistemic State (`epistemic_state.md`)

Each entry keyed by stable ID:

```markdown
## E007: 2x_pullback edge (CONTESTED)

**Position:** The 74% WR from #1343 is DEBUNKED. Corrected baseline is 48.9%
(#1404). #1418 hypothesizes 69.7% at tick-level — UNVERIFIED pending broker data.

**Evidence:**
- #1343: "74% WR" → REFUTED by #1404 (same-bar resolution bug)
- #1404: "48.9% WR" → CURRENT BASELINE
- #1418: "69.7% tick-level" → UNVERIFIED, needs broker fill data

**Related concepts:** C042 (check_level_hits), C015 (2x_pullback_signal)
**Agent guidance:** Do NOT build features assuming the edge exists. Use 48.9% baseline.
```

**Fields:**
- `Position:` — current understanding (1-2 sentences)
- `Evidence:` — chronological chain of claims and what resolved them
- `Related concepts:` — by concept ID
- `Agent guidance:` — 1 sentence, actionable directive. Omit if it would just repeat position.
- `Status:` — believed / refuted / contested / unverified

### Workflow Registry (`workflow_registry.md`)

Each entry keyed by stable ID:

```markdown
## W007: Investigation Protocol (CURRENT)
- **Context:** How agents investigate bugs and unexpected behavior
- **Current method:** Deterministic debugging — trace against real data, never speculate
- **Supersedes:** W002 (code-reading speculation)
- **Trigger for change:** #1404 — spent 3 sessions theorizing, traced root cause in 10 min
- **Lesson:** Execute against real data. Theories from code reading alone missed edge cases.
- **Repetitions detected:** 6 instances of agents defaulting to code-reading before this was formalized

## W012: Agent Handoff (CURRENT — HAS DEBT)
- **Context:** How work transfers between agent sessions
- **Current method:** sm send with structured messages
- **Debt:** No schema for handoff messages — agents interpret freely
- **Repetition:** 4 instances of agents waiting indefinitely on ambiguous handoffs

## W003: Manual Code Review (SUPERSEDED) → W007
```

**Fields:**
- `Context:` — what problem this workflow addresses
- `Current method:` — how it works now
- `Supersedes:` — by workflow ID (what it replaced)
- `Trigger for change:` — what caused the evolution
- `Lesson:` — what was learned
- `Repetitions detected:` — recurring patterns that prompted formalization
- `Debt:` — known gaps in the workflow
- `Status:` — CURRENT / SUPERSEDED → W{other} / MERGED → W{other}

### Timeline (`timeline.md`)

Chronological narrative, not a changelog. References concepts and claims by ID.

```markdown
## Phase: Early Architecture (2024-Q4)

Built initial swing detection with LegDetector (C001). Single-timeframe,
bar-close processing. First backtest showed promising results (E001: 74% WR,
later refuted). Investigation methodology was ad-hoc (W002: code-reading
speculation, later superseded by W007).

Key artifacts: swing_detection_rewrite_spec.md, LegDetector initial impl
```

### Graveyard Files

**`concept_graveyard.md`** — full entries for DEAD/EVOLVED concepts:

```markdown
## C012: proximity_pruning (DEAD)
- **Died:** ~#1100s era
- **Residue:** CLAUDE.md LegPruner description still mentions it
- **Replaced by:** C023 (structure-driven pruning via breach + turn ratio)
- **Lesson:** Proximity alone was too aggressive — pruned valid structure
- **Archive:** proximity_pruning_redesign.md, proximity_pruning_perf.md
```

**`epistemic_graveyard.md`** — full entries for refuted claims:

```markdown
## E001: 74% win rate claim (REFUTED)
- **Claimed in:** #1343 (2026-02-06)
- **Refuted by:** #1404 (2026-02-08)
- **Mechanism:** Same-bar resolution bug counted impossible trades as wins
- **Corrected value:** 48.9% (see E007)
- **Residue:** #1343 tables, some analysis scripts
```

Both files are append-only. Write once when status flips, never modify.

---

## Standalone Repo Design

The knowledge server is project-agnostic. Standalone public repo.

| Standalone repo (public) | Project repo (stays) |
|---|---|
| Server daemon / CLI | 4 living docs + 2 graveyard files |
| Fold agent prompt templates | `local_data/` (chunks, queue, issues) |
| Schema linter | Config file (paths, thresholds, model) |
| Doc schemas + examples | `.claude/` integration hooks |
| This methodology doc (README) | Project-specific CLAUDE.md briefing |

### Project Config

Each project has a config file pointing to its knowledge store:

```yaml
# .knowledge/config.yaml (or similar)
living_docs:
  timeline: docs/decisions/timeline.md
  concepts: docs/decisions/concept_registry.md
  epistemic: docs/decisions/epistemic_state.md
  workflows: docs/decisions/workflow_registry.md

graveyard:
  concepts: docs/decisions/concept_graveyard.md
  epistemic: docs/decisions/epistemic_graveyard.md

briefing: # L0 target
  file: CLAUDE.md
  section: "## Project Knowledge Briefing"

sources:
  issues: local_data/issues/
  sessions: local_data/sessions/
  docs:
    - docs/working/
    - docs/archive/
    - docs/specs/

thresholds:
  orphan_triage: 50
  contested_review_days: 14
  stale_unverified_days: 30
  workflow_repetition: 3

budget:
  context_limit_chars: 600000
  instructions_overhead: 10000
  max_chunk_chars: 200000

model: sonnet  # or any LLM with file editing
```

---

## Cost and Failure Design

### Cost Model

| Phase | Cost | Frequency |
|-------|------|-----------|
| Bootstrap seed | $0.50-1 | Once per project |
| Bootstrap fold (if used) | $5-25 depending on depth | Once per project |
| Steady-state dispatch | $0.10-0.15 per chunk | 1-5/week during active development |
| L0 briefing regen | $0.01-0.02 (Haiku-class) | After each dispatch |
| Retries (~20% rate) | $0.10 per retry | ~1 in 5 dispatches |

Steady-state operational cost: **$0.50-4/month** during active development. $0 when idle.

### Failure Recovery

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Schema violation | Linter (automatic) | Auto-retry with correction prompt (max 2) |
| Diff too large (>2x expected) | Size guard (automatic) | Flag for human review |
| Agent truncates output | Missing expected sections | Auto-retry |
| Agent fabricates content | Harder to detect | Spot-check at human checkpoints |
| Agent dies mid-chunk | No commit produced | Re-send prompt file (self-contained) |
| Out-of-order processing | N/A | Acceptable — IDs are pre-assigned, surgical edits don't conflict |

Design principle: **make failure cheap and recovery automatic.** Schema linter + auto-retry handles most cases. Human review is the exception.

---

## Deliverables

1. [ ] Standalone repo with server, linter, prompt templates, config schema
2. [ ] Stable ID migration for existing living docs (one-time, from v2)
3. [ ] 4th living doc: workflow_registry.md
4. [ ] Graveyard files + compaction migration
5. [ ] Bootstrap seed mode (snapshot + incremental)
6. [ ] Server daemon (watch → accumulate → dispatch → validate)
7. [ ] L0 briefing auto-generation
8. [ ] Integration hooks for project repos (file_issue skill, etc.)

---

## v2 Implementation History

Preserved for context. The v2 fold processed 17 chunks (Oct–Jan 1) using a manual script model.

### v1 (discarded)
- 5 chunks processed (Dec 10-18). Docs hit 130K chars — too verbose.
- Agent wrote narrative prose, long concept entries, repeated boilerplate.

### v2 (17 chunks completed)
- 17 chunks processed (Dec 10 → Jan 1). Docs at ~278K total.
- 38% more compact than v1 for same content coverage.
- Worktree: `~/Desktop/fractal-knowledge-fold` (branch: `knowledge-fold`)
- Agent session: `4c17f464` (name: `knowledge-fold-chunk-4`)

**Key v2 learnings (informing v3 design):**

1. **Verbose output kills budget.** v1 wrote narrative prose. "Be succinct" cut output 38%. → v3 enforces line limits via schema linter.
2. **Git diff is ground truth for liveness.** Without codebase change data, agent can't know concepts are dead. → v3 makes drift detection central.
3. **Worktree isolation is essential.** Other agents switching branches deleted uncommitted docs. → v3 server manages its own workspace.
4. **Self-contained prompts prevent context loss.** → v3 server always writes complete prompt files.
5. **Cold compaction alone can't keep up.** Growth is ~15-20K/chunk, compaction saves ~1-4K. The real win is orphan triage. → v3 generalizes triage to all drift types.
6. **Agent fabricates excuses for orphans.** "Different source tree" when frontend/ is the same repo. → v3 stable IDs make orphan detection deterministic.
7. **Meta-commentary creeps in.** Agent wrote "Chunk N summary" blocks about the fold process. → v3 schema linter rejects entries that don't match expected categories.
8. **Epistemic entries too verbose.** → v3 caps agent guidance at 1 sentence via schema enforcement.
9. **Budget must account for living doc growth.** Fixed budget caused OOM. → v3 dynamic budget with graveyard preventing unbounded growth.
10. **Failed chunks are recoverable.** Prompt files are self-contained, out-of-order processing works. → v3 server automates this.

---

## Design Rationale

### Why not RAG?

RAG answers "find me documents about X." This system answers "what do we know, what's dead, and what contradicts what." RAG finds similarity, not contradiction. It doesn't capture relationships, doesn't know what's dead, and retrieves chunks rather than pre-synthesized knowledge. At scale (2,000+ docs), RAG as an L2 fallback may become useful. Not there yet.

### Why a server, not a script?

A script requires human initiative. The moment development gets busy, knowledge drifts. A server makes maintenance ambient: the same events that cause drift trigger the updates that resolve it. Cost is proportional to change rate, not time.

### Why separate liveness from validity?

Code concepts (alive/dead) and epistemic claims (believed/refuted) have different lifecycles and different resolution mechanisms. Mixing them lets agents treat claims as if code drift can kill them, or treat code as if it can be "believed." Separate ontologies prevent category errors.

### Why stable IDs?

File paths and function names change — especially with AI-assisted refactoring. The system's own success accelerates this drift. Stable IDs decouple concept identity from code location, making EVOLVED vs DEAD decisions unambiguous and reducing agent fabrication.

### Why lossless compaction (graveyard)?

Stripping dead content in-place preserves it only in git history, which agents can't query cheaply. Graveyard files are append-only, ID-keyed, and immediately readable. Living docs stay concise; historical detail stays accessible.

### Why workflows as a 4th dimension?

Timeline, concepts, and epistemic state capture what happened, what exists, and what we believe. Missing: how we work. Workflows evolve constantly and their evolution contains patterns (repetitions, failures, formalizations) that inform better process decisions. No individual session can see this — only the longitudinal view catches it.

### Why drift-centric scheduling?

In steady state, most knowledge is unchanged. Spending tokens on stable entries is waste. Prioritizing drift resolution (orphans, contradictions, stale claims) focuses cost on the only things that matter: keeping the docs accurate.

### Why chronological order for bootstrap?

The chronological sequence carries lifecycle information that batch processing loses. Concepts are born, evolve, and die over time. Processing in order lets the fold agent see a concept in the registry and update it naturally. After bootstrap, the server's event-driven model takes over.

### Why full docs + separate compression?

Investigation needs full detail (L1). Session initialization needs a compressed briefing (L0). Different consumers, different needs. The server generates L0 from L1 after each dispatch — a cheap Haiku-class call.

### Why project config, not convention?

Different projects have different directory structures, different doc locations, different threshold needs. A config file makes the server truly project-agnostic. Convention-based defaults keep setup minimal for standard layouts.

---

## Ticket Classification

**EPIC.** This requires a standalone repo, server daemon, schema linter, stable ID migration, 4th doc creation, graveyard migration, bootstrap seed mode, L0 auto-generation, and project integration hooks. No single agent completes this in one context window. Sub-tickets to be filed after spec approval.
