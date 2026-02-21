# Engram

Persistent memory for AI coding agents.

## The Problem

AI coding agents start every session with amnesia. They read your codebase, your docs, your issues — and build a mental model from scratch. Every time.

This means they:

- **Use dead concepts confidently.** A technique you abandoned 3 months ago still appears in old docs and code comments. The agent picks it up and builds on it.
- **Miss contradictions.** Three different docs claim three different win rates for the same strategy. The agent reads one and treats it as truth.
- **Can't see relationships.** Two features are semantically the same thing in different event loops. Keyword search won't find the connection. The overlap is architectural, not textual.
- **Build on false premises.** Some claims in your docs are believed, some refuted, some contested pending evidence. Without knowing which is which, agents make decisions on bad foundations.
- **Repeat workflow mistakes.** Your investigation methodology evolved through painful trial and error. Every new session starts from zero, re-learning lessons you've already paid for.

You compensate by re-explaining context every session. Knowledge evaporates when sessions end. You are the memory.

## The Idea

A lightweight server that watches your repo and continuously maintains 4 living knowledge documents:

| Document | Captures |
|----------|----------|
| **Timeline** | What happened — project evolution as narrative |
| **Concept Registry** | What exists — every code concept, alive or dead, with stable IDs |
| **Epistemic State** | What we believe — claims, contradictions, evidence chains |
| **Workflow Registry** | How we work — processes, their evolution, repeated patterns |

When changes accumulate (commits, issues, doc updates), the server dispatches a fold agent to update these docs. A schema linter validates the result. A compressed briefing is regenerated for your CLAUDE.md / AGENTS.md / similar.

Every agent session starts with living docs that reflect yesterday's reality — not last month's, not last quarter's. The gap between "what the project knows" and "what the agent knows" is bounded by hours, not sessions.

### How It Works

```
Your repo events (git push, issue filed, PR merged)
    │
    ▼
Engram server (watches, accumulates, no LLM calls)
    │
    ▼  buffer fills
Fold agent (any LLM with file editing)
    │
    ▼  schema linter validates
4 living docs updated + L0 briefing regenerated
```

**Cost:** Buffer construction is pure CPU. LLM calls happen only at dispatch. Steady-state: $0.50-4/month during active development. $0 when idle.

### Key Design Decisions

- **Stable IDs over file paths.** Concepts get permanent IDs (`C042`). File paths are secondary pointers. A concept can be refactored ten times without "dying."
- **Liveness ≠ validity.** Code concepts are alive/dead/evolved. Epistemic claims are believed/refuted/contested. These are different categories with different lifecycles. The schema enforces the boundary.
- **Drift drives scheduling.** Orphaned concepts, unresolved contradictions, and stale claims get priority over chronological processing. Tokens are spent where accuracy is at risk.
- **Compaction is lossless.** Dead concepts and refuted claims move to append-only graveyard files. Living docs stay concise. Historical detail stays queryable.
- **Model-agnostic.** The fold agent is any LLM that can read a file and make edits. Claude Code, Codex, or equivalent.

## Status

Design phase. See [engram_idea.md](engram_idea.md) for the full spec including architecture, document schemas, bootstrap strategies, and design rationale.

## Getting Started

Not yet implemented. The spec is the current deliverable. If you're interested in this problem space, read the idea doc and open an issue.

Session source formats currently supported in config:
- `claude-code`: `~/.claude/history.jsonl`
- `codex`: `~/.codex/history.jsonl` (plus `~/.codex/sessions/**` for project-path matching)
