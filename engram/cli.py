"""CLI entry point for engram."""

from __future__ import annotations

from pathlib import Path

import click


# Schema headers for living docs
LIVING_DOC_HEADERS: dict[str, str] = {
    "timeline": (
        "# Timeline\n"
        "\n"
        "Chronological narrative of project evolution. "
        "References concepts (C###), claims (E###), and workflows (W###) by stable ID.\n"
    ),
    "concepts": (
        "# Concept Registry\n"
        "\n"
        "Code concepts keyed by stable ID (C###). "
        "Status: ACTIVE / DEAD / EVOLVED.\n"
    ),
    "epistemic": (
        "# Epistemic State\n"
        "\n"
        "Claims and beliefs keyed by stable ID (E###). "
        "Status: believed / refuted / contested / unverified.\n"
    ),
    "workflows": (
        "# Workflow Registry\n"
        "\n"
        "Process patterns keyed by stable ID (W###). "
        "Status: CURRENT / SUPERSEDED / MERGED.\n"
    ),
}

GRAVEYARD_HEADERS: dict[str, str] = {
    "concepts": (
        "# Concept Graveyard\n"
        "\n"
        "Append-only archive of DEAD and EVOLVED concept entries. "
        "Keyed by stable ID (C###).\n"
    ),
    "epistemic": (
        "# Epistemic Graveyard\n"
        "\n"
        "Append-only archive of refuted claims. "
        "Keyed by stable ID (E###).\n"
    ),
}

# Default config template
CONFIG_TEMPLATE = """\
living_docs:
  timeline: docs/decisions/timeline.md
  concepts: docs/decisions/concept_registry.md
  epistemic: docs/decisions/epistemic_state.md
  workflows: docs/decisions/workflow_registry.md

graveyard:
  concepts: docs/decisions/concept_graveyard.md
  epistemic: docs/decisions/epistemic_graveyard.md

briefing:
  file: CLAUDE.md
  section: "## Project Knowledge Briefing"

sources:
  issues: local_data/issues/
  docs:
    - docs/working/
    - docs/archive/
    - docs/specs/
  sessions:
    format: claude-code
    path: ~/.claude/history.jsonl
    project_match: []

thresholds:
  orphan_triage: 50
  contested_review_days: 14
  stale_unverified_days: 30
  workflow_repetition: 3

budget:
  context_limit_chars: 600000
  instructions_overhead: 10000
  max_chunk_chars: 200000

model: sonnet
"""


@click.group()
def cli() -> None:
    """Engram: persistent memory for AI coding agents."""


@cli.command()
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
def init(project_root: str) -> None:
    """Initialize .engram/ directory with config and empty living docs."""
    root = Path(project_root)
    engram_dir = root / ".engram"

    if engram_dir.exists():
        click.echo(f".engram/ already exists at {engram_dir}")
        raise SystemExit(1)

    # Create .engram/ and config
    engram_dir.mkdir(parents=True)
    config_path = engram_dir / "config.yaml"
    config_path.write_text(CONFIG_TEMPLATE)
    click.echo(f"Created {config_path}")

    # Parse config to get doc paths
    import yaml
    config = yaml.safe_load(CONFIG_TEMPLATE)

    # Create living docs with schema headers
    for key, rel_path in config["living_docs"].items():
        doc_path = root / rel_path
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(LIVING_DOC_HEADERS[key])
        click.echo(f"Created {doc_path}")

    # Create graveyard files with schema headers
    for key, rel_path in config["graveyard"].items():
        doc_path = root / rel_path
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(GRAVEYARD_HEADERS[key])
        click.echo(f"Created {doc_path}")

    click.echo("\nEngram initialized. Edit .engram/config.yaml to customize paths.")
