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

    # Load config through the standard path to validate it
    from engram.config import load_config
    config = load_config(root)

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


@cli.command("build-queue")
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
def build_queue_cmd(project_root: str) -> None:
    """Build chronological queue of all project artifacts."""
    from engram.config import load_config
    from engram.fold.queue import build_queue

    root = Path(project_root)
    config = load_config(root)
    entries = build_queue(config, root)

    doc_count = sum(1 for e in entries if e["type"] == "doc")
    revisit_count = sum(
        1 for e in entries if e["type"] == "doc" and e["pass"] == "revisit"
    )
    issue_count = sum(1 for e in entries if e["type"] == "issue")
    session_count = sum(1 for e in entries if e["type"] == "prompts")

    click.echo(f"Built queue: {len(entries)} entries")
    click.echo(f"  Docs: {doc_count} ({revisit_count} revisits)")
    click.echo(f"  Issues: {issue_count}")
    click.echo(f"  Sessions: {session_count}")

    if entries:
        first = entries[0]["date"][:10]
        last = entries[-1]["date"][:10]
        click.echo(f"  Date range: {first} to {last}")


@cli.command("next-chunk")
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
def next_chunk_cmd(project_root: str) -> None:
    """Build the next chunk input and prompt files."""
    from engram.config import load_config
    from engram.fold.chunker import next_chunk

    root = Path(project_root)
    config = load_config(root)

    try:
        result = next_chunk(config, root)
    except FileNotFoundError as exc:
        click.echo(str(exc))
        raise SystemExit(1)
    except ValueError as exc:
        click.echo(str(exc))
        raise SystemExit(0)

    click.echo(f"Chunk {result.chunk_id}:")
    click.echo(f"  Type: {result.chunk_type}")
    click.echo(f"  Living docs: {result.living_docs_chars:,} chars")
    click.echo(f"  Budget: {result.budget:,} chars")

    if result.chunk_type == "fold":
        click.echo(f"  Items: {result.items_count}")
        click.echo(f"  Chunk chars: {result.chunk_chars:,}")
        click.echo(f"  Date range: {result.date_range}")
        if result.pre_assigned_ids:
            for cat, ids in result.pre_assigned_ids.items():
                click.echo(f"  Pre-assigned {cat}: {ids}")
    else:
        click.echo(f"  Drift entries: {result.drift_entry_count}")
        click.echo("  ** Drift triage round — no queue items consumed **")

    click.echo(f"  Written: {result.input_path}")
    click.echo(f"  Prompt: {result.prompt_path}")
    click.echo(f"  Remaining in queue: {result.remaining_queue}")


@cli.command()
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
def lint(project_root: str) -> None:
    """Validate living docs against schema rules."""
    from engram.config import load_config
    from engram.linter import lint_from_paths

    root = Path(project_root)
    config = load_config(root)
    result = lint_from_paths(root, config)

    if result.passed:
        click.echo("Lint: PASS (0 violations)")
    else:
        click.echo(f"Lint: FAIL ({len(result.violations)} violations)")
        for v in result.violations:
            loc = v.doc_type
            if v.entry_id:
                loc += f"/{v.entry_id}"
            click.echo(f"  [{loc}] {v.message}")
        raise SystemExit(1)
