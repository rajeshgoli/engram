# CLAUDE.md

**GitHub:** rajeshgoli/engram

## Project Overview

Engram is a knowledge server that gives AI coding agents persistent memory. It watches a project repo for changes, accumulates context, and dispatches fold agents to maintain living knowledge documents. Agents start every session with near-current understanding instead of amnesia.

## Development Commands

### Setup
```bash
python -m venv venv && source venv/bin/activate && pip install -e ".[dev]"
```

### Testing
```bash
source venv/bin/activate && python -m pytest tests/ -v
```

### Branch Protection (CRITICAL)

**Main branch is protected. Violations trigger CI failure.**
- **NEVER push to main**: `git push origin main` is FORBIDDEN
- **ALL work** goes through: feature branch → PR → dev
Merging to main is a human-only operation for releases.

## Architecture

```
engram/
├── server/          # Watch → accumulate → dispatch → validate loop
├── fold/            # Chunk building, prompt templates, agent dispatch
├── linter/          # Schema validation + invariant checks
├── bootstrap/       # Seed (snapshot) + historical fold
└── cli.py           # CLI entry point (engram init, engram seed, engram status)
```

### Key Concepts

- **Living docs**: 4 markdown files maintained per project (timeline, concepts, epistemic, workflows)
- **Stable IDs**: C###/E###/W### — primary keys for concepts, claims, workflows
- **Graveyard files**: Append-only archives for DEAD/refuted entries
- **L0 briefing**: Compressed summary auto-injected into project's CLAUDE.md
- **Fold agent**: Any LLM with file editing (Claude Code, Codex, etc.)

### Design Spec

Read `engram_idea.md` for the full v3 design: architecture, schemas, bootstrap, rationale.

## Role-Based Workflows

When asked to work as a specific role:
1. Read `.agent-os/personas/[role].md` first
2. Follow the workflow defined there
3. Stay in role — **Architect and Scout do NOT make code changes** unless explicitly asked

## Specs & Working Docs

Specs live in `specs/`. Name convention: `<ticket#>_<descriptive_name>.md`
