Read .agent-os/agents.md for workflow instructions and persona definitions.

# Engram

**GitHub:** rajeshgoli/engram

Persistent memory for AI coding agents. Watches repos for changes, maintains living knowledge docs, dispatches fold agents.

## Development Commands

```bash
# Setup
python -m venv venv && source venv/bin/activate && pip install -e ".[dev]"

# Test
source venv/bin/activate && python -m pytest tests/ -v
```

## Branch Protection

- NEVER push to main — ALL work goes through feature branch → PR → dev
- Merging to main is a human-only operation

## Architecture

```
engram/
├── server/          # Watch → accumulate → dispatch → validate loop
├── fold/            # Chunk building, prompt templates, agent dispatch
├── linter/          # Schema validation + invariant checks
├── bootstrap/       # Seed (snapshot) + historical fold
└── cli.py           # CLI entry point
```

## Design Spec

Read `engram_idea.md` for the full design.

## Specs

Implementation specs live in `specs/`.
