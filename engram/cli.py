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
    format: claude-code  # Built-in: claude-code, codex
    path: ~/.claude/history.jsonl  # codex default: ~/.codex/history.jsonl
    project_match: []

thresholds:
  orphan_triage: 50
  contested_review_days: 14
  stale_unverified_days: 30
  stale_epistemic_days: 90
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
@click.option(
    "--start-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Only include entries on or after this date (YYYY-MM-DD).",
)
def build_queue_cmd(project_root: str, start_date: object) -> None:
    """Build chronological queue of all project artifacts."""
    from engram.config import load_config
    from engram.fold.queue import build_queue
    from engram.server.db import ServerDB

    root = Path(project_root)
    config = load_config(root)

    # Resolve start_date: explicit flag > DB marker > None
    db = ServerDB(root / ".engram" / "engram.db")
    fold_from = db.get_fold_from()

    if start_date:
        effective_start = start_date.date().isoformat()  # type: ignore[union-attr]
    elif fold_from:
        effective_start = fold_from
    else:
        effective_start = None

    entries = build_queue(config, root, start_date=effective_start)

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
    from engram.server.db import ServerDB

    root = Path(project_root)
    config = load_config(root)

    _enforce_single_active_chunk(root)

    db = ServerDB(root / ".engram" / "engram.db")
    fold_from = db.get_fold_from()

    try:
        result = next_chunk(config, root, fold_from=fold_from)
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

    _write_active_chunk_lock(root, result)


@cli.command("clear-active-chunk")
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
def clear_active_chunk_cmd(project_root: str) -> None:
    """Clear the active chunk lock (recovery for aborted/failed chunk processing)."""
    root = Path(project_root)
    lock_path = _active_chunk_lock_path(root)
    if lock_path.exists():
        lock_path.unlink()
        click.echo(f"Cleared active chunk lock: {lock_path}")
    else:
        click.echo("No active chunk lock present.")


def _active_chunk_lock_path(project_root: Path) -> Path:
    return project_root / ".engram" / "active_chunk.yaml"


def _enforce_single_active_chunk(project_root: Path) -> None:
    """Prevent generating multiple chunks before processing the active one."""
    import re
    import subprocess

    import yaml

    lock_path = _active_chunk_lock_path(project_root)
    if not lock_path.exists():
        return

    try:
        lock = yaml.safe_load(lock_path.read_text()) or {}
    except OSError:
        return

    chunk_id = lock.get("chunk_id")
    if not isinstance(chunk_id, int):
        return

    # Auto-clear lock if git history indicates the chunk was processed.
    # This is a best-effort heuristic for CLI-driven workflows.
    try:
        ok = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
        inside = ok.returncode == 0 and ok.stdout.strip() == "true"
    except OSError:
        inside = False

    if inside:
        try:
            log = subprocess.run(
                ["git", "log", "-n", "200", "--format=%s"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                check=False,
            )
            subjects = log.stdout or ""
        except OSError:
            subjects = ""

        if re.search(rf"Knowledge fold: chunk(?:_| )0*{chunk_id}\b", subjects):
            lock_path.unlink()
            return

    input_path = lock.get("input_path", "<unknown>")
    raise click.ClickException(
        "Active chunk lock present. Process the existing chunk before generating a new one:\n"
        f"  chunk_id: {chunk_id}\n"
        f"  input: {input_path}\n"
        "To override (abandon and regenerate), run:\n"
        f"  engram clear-active-chunk --project-root {project_root}\n"
    )


def _write_active_chunk_lock(project_root: Path, result: object) -> None:
    from datetime import datetime, timezone

    import yaml

    lock_path = _active_chunk_lock_path(project_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chunk_id": getattr(result, "chunk_id", None),
        "chunk_type": getattr(result, "chunk_type", None),
        "input_path": str(getattr(result, "input_path", "")),
        "prompt_path": str(getattr(result, "prompt_path", "")),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    lock_path.write_text(yaml.safe_dump(payload, sort_keys=True))


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


@cli.command()
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
@click.option(
    "--fold-from",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Set fold continuation marker date (YYYY-MM-DD).",
)
def migrate(project_root: str, fold_from: object) -> None:
    """Migrate v2 living docs to v3 format (one-time)."""
    from engram.migrate import migrate as run_migrate

    root = Path(project_root)
    fold_date = fold_from.date() if fold_from else None  # type: ignore[union-attr]

    click.echo("Starting v2 \u2192 v3 migration...")
    lint_result, counters = run_migrate(root, fold_date)

    click.echo(f"Counter state: C={counters['C']}, E={counters['E']}, W={counters['W']}")

    if fold_date:
        click.echo(f"Fold continuation marker set: {fold_date.isoformat()}")

    if lint_result.passed:
        click.echo("Validation: PASS (0 violations)")
        click.echo("Migration complete.")
    else:
        click.echo(f"Validation: FAIL ({len(lint_result.violations)} violations)")
        for v in lint_result.violations:
            loc = v.doc_type
            if v.entry_id:
                loc += f"/{v.entry_id}"
            click.echo(f"  [{loc}] {v.message}")
        click.echo("Migration complete with validation warnings.")
        raise SystemExit(1)


@cli.command("migrate-epistemic-history")
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
def migrate_epistemic_history(project_root: str) -> None:
    """Externalize inline epistemic History blocks into inferred per-ID files."""
    from engram.config import load_config, resolve_doc_paths
    from engram.migrate_epistemic_history import externalize_epistemic_history

    root = Path(project_root)
    config = load_config(root)
    doc_paths = resolve_doc_paths(config, root)
    epistemic_path = doc_paths["epistemic"]

    result = externalize_epistemic_history(epistemic_path)
    click.echo("Epistemic history migration complete.")
    click.echo(f"  Migrated entries: {result.migrated_entries}")
    click.echo(f"  Created files: {result.created_files}")
    click.echo(f"  Appended blocks: {result.appended_blocks}")

    from engram.linter import lint_from_paths
    lint_result = lint_from_paths(root, config)
    if lint_result.passed:
        click.echo("  Lint: PASS (0 violations)")
    else:
        click.echo(f"  Lint: FAIL ({len(lint_result.violations)} violations)")
        for v in lint_result.violations:
            loc = v.doc_type
            if v.entry_id:
                loc += f"/{v.entry_id}"
            click.echo(f"    [{loc}] {v.message}")
        raise SystemExit(1)


@cli.command()
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
def run(project_root: str) -> None:
    """Run the engram knowledge server (foreground)."""
    import logging

    from engram.config import load_config
    from engram.server import run_server

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = Path(project_root)
    config = load_config(root)
    click.echo(f"Starting engram server for {root}...")
    run_server(config, root)


@cli.command()
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
def status(project_root: str) -> None:
    """Show engram server status."""
    from engram.config import load_config
    from engram.server import get_status

    root = Path(project_root)
    config = load_config(root)
    info = get_status(config, root)

    if "error" in info:
        click.echo(f"Error: {info['error']}")
        raise SystemExit(1)

    # Buffer
    buf = info["buffer"]
    click.echo("Buffer:")
    click.echo(f"  Items: {buf['item_count']}")
    click.echo(f"  Chars: {buf['buffer_chars']:,} / {buf['budget']:,} ({buf['fill_pct']:.1f}%)")
    click.echo(f"  Living docs: {buf['living_docs_chars']:,} chars")

    # Pending items
    click.echo(f"\nPending items: {info['pending_items']}")

    # Last dispatch
    last = info.get("last_dispatch")
    if last:
        click.echo(f"\nLast dispatch:")
        click.echo(f"  Chunk: {last['chunk_id']}")
        click.echo(f"  State: {last['state']}")
        click.echo(f"  Retries: {last['retry_count']}")
        click.echo(f"  Time: {last['updated_at']}")
        if last.get("error"):
            click.echo(f"  Error: {last['error']}")
    else:
        click.echo("\nNo dispatches yet.")

    # Recent dispatch history
    recent = info.get("recent_dispatches", [])
    if recent:
        click.echo(f"\nRecent dispatches ({len(recent)}):")
        for d in recent:
            err = f" [{d['error'][:40]}...]" if d.get("error") else ""
            click.echo(
                f"  #{d['chunk_id']} {d['state']} "
                f"(retries={d['retry_count']}) {d['updated_at']}{err}"
            )

    # Server state
    state = info.get("server_state", {})
    if state.get("last_poll_time"):
        click.echo(f"\nLast poll: {state['last_poll_time']}")
    if state.get("last_dispatch_time"):
        click.echo(f"Last dispatch: {state['last_dispatch_time']}")


@cli.command()
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
@click.option(
    "--from-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Seed from historical snapshot at this date, then fold forward (Path A).",
)
def seed(project_root: str, from_date: object) -> None:
    """Bootstrap: seed living docs from repo snapshot.

    Without --from-date (Path B): seeds from current repo state.
    With --from-date (Path A): checks out snapshot at date, seeds, folds forward.
    """
    import logging

    from engram.bootstrap.seed import seed as run_seed

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = Path(project_root)
    seed_date = from_date.date() if from_date else None  # type: ignore[union-attr]

    if seed_date:
        click.echo(f"Seeding from snapshot at {seed_date}...")
    else:
        click.echo("Seeding from current repo state...")

    success = run_seed(root, from_date=seed_date)

    if success:
        click.echo("Seed complete.")
    else:
        click.echo("Seed failed.")
        raise SystemExit(1)


@cli.command()
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (default: cwd).",
)
@click.option(
    "--from",
    "from_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    required=True,
    help="Process artifacts from this date forward (YYYY-MM-DD).",
)
def fold(project_root: str, from_date: object) -> None:
    """Forward fold: process artifacts from a date to today.

    Builds the queue, filters to entries >= the given date, then processes
    each chunk through the fold agent. Used after migration (Path C) or
    internally by seed --from-date (Path A).
    """
    import logging

    from engram.bootstrap.fold import forward_fold

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = Path(project_root)
    fold_date = from_date.date()  # type: ignore[union-attr]

    click.echo(f"Forward fold from {fold_date}...")
    success = forward_fold(root, fold_date)

    if success:
        click.echo("Forward fold complete.")
    else:
        click.echo("Forward fold completed with errors.")
        raise SystemExit(1)
