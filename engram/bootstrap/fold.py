"""Forward fold: process artifacts chronologically from a start date to today.

Reuses the queue builder (:mod:`engram.fold.queue`) and chunker
(:mod:`engram.fold.chunker`) with date filtering. Iterates:
build queue → filter by date → chunk → dispatch → validate → repeat
until the queue is exhausted.

Used by:
- **Path A** (internally after seed): ``seed --from-date`` calls this.
- **Path C** (after migration): ``engram fold --from YYYY-MM-DD``.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from engram.config import load_config, resolve_doc_paths
from engram.dispatch import invoke_agent, read_docs
from engram.fold.chunker import ChunkResult, next_chunk
from engram.fold.queue import build_queue
from engram.linter import lint_post_dispatch
from engram.server.db import ServerDB

log = logging.getLogger(__name__)

MAX_RETRIES = 2


def _filter_queue_by_date(
    project_root: Path,
    from_date: date,
) -> int:
    """Filter the queue JSONL to only include entries on or after *from_date*.

    Modifies the queue file in place. Returns the number of entries remaining.
    """
    queue_file = project_root / ".engram" / "queue.jsonl"
    if not queue_file.exists():
        return 0

    cutoff = from_date.isoformat()
    with open(queue_file) as fh:
        entries = [json.loads(line) for line in fh if line.strip()]

    filtered = [e for e in entries if e["date"][:10] >= cutoff]

    with open(queue_file, "w") as fh:
        for entry in filtered:
            fh.write(json.dumps(entry) + "\n")

    removed = len(entries) - len(filtered)
    if removed:
        log.info("Filtered queue: removed %d entries before %s, %d remaining",
                 removed, from_date, len(filtered))
    return len(filtered)


def _build_prompt(chunk: ChunkResult, correction_text: str | None = None) -> str:
    """Read the chunk prompt and optionally append correction context."""
    prompt = chunk.prompt_path.read_text()
    if correction_text:
        prompt = prompt + "\n\n" + correction_text
    return prompt


def _dispatch_and_validate(
    config: dict[str, Any],
    project_root: Path,
    chunk: ChunkResult,
) -> bool:
    """Dispatch a chunk to the fold agent, lint, retry on failure.

    Returns True if the chunk was processed successfully.
    """
    doc_paths = resolve_doc_paths(config, project_root)
    before_contents = read_docs(
        doc_paths, ("timeline", "concepts", "epistemic", "workflows"),
    )

    correction_text: str | None = None

    for attempt in range(1 + MAX_RETRIES):
        if attempt > 0:
            log.info("Retry %d/%d for chunk %d", attempt, MAX_RETRIES, chunk.chunk_id)

        prompt = _build_prompt(chunk, correction_text)
        ok = invoke_agent(config, project_root, prompt)
        if not ok:
            continue

        # Validate with linter
        after_contents = read_docs(
            doc_paths, ("timeline", "concepts", "epistemic", "workflows"),
        )
        graveyard_docs = read_docs(
            doc_paths, ("concept_graveyard", "epistemic_graveyard"),
        )

        pre_assigned: list[str] = []
        for id_list in chunk.pre_assigned_ids.values():
            pre_assigned.extend(id_list)

        result = lint_post_dispatch(
            before_contents=before_contents,
            after_contents=after_contents,
            graveyard_docs=graveyard_docs,
            pre_assigned_ids=pre_assigned if pre_assigned else None,
            expected_growth=chunk.chunk_chars,
            config=config,
        )

        if result.passed:
            return True

        # Build correction for retry
        violations_text = "\n".join(
            f"- [{v.doc_type}/{v.entry_id or ''}] {v.message}"
            for v in result.violations
        )
        correction_text = (
            f"CORRECTION REQUIRED: Previous attempt had "
            f"{len(result.violations)} lint violations:\n\n"
            f"{violations_text}\n\n"
            f"Please fix these violations in the living docs.\n"
        )
        log.warning(
            "Lint failed (%d violations) for chunk %d",
            len(result.violations), chunk.chunk_id,
        )

    return False


def forward_fold(
    project_root: Path,
    from_date: date,
    config: dict[str, Any] | None = None,
) -> bool:
    """Run a forward fold from *from_date* to today.

    Builds the queue, filters to entries >= from_date, then iterates
    through chunks dispatching each to the fold agent.

    Parameters
    ----------
    project_root:
        Project root with ``.engram/config.yaml``.
    from_date:
        Only process artifacts dated on or after this date.
    config:
        Pre-loaded config. If None, loads from project_root.

    Returns
    -------
    bool
        True if all chunks processed successfully.
    """
    if config is None:
        config = load_config(project_root)

    # Step 1: Build the full queue
    log.info("Building queue...")
    entries = build_queue(config, project_root)
    log.info("Queue built: %d total entries", len(entries))

    # Step 2: Filter by date
    remaining = _filter_queue_by_date(project_root, from_date)
    if remaining == 0:
        log.info("No entries to process after %s", from_date)
        db = ServerDB(project_root / ".engram" / "engram.db")
        db.clear_fold_from()
        return True

    log.info("Processing %d entries from %s forward", remaining, from_date)

    # Step 3: Iterate chunks until queue exhausted
    chunk_count = 0
    failures = 0

    while True:
        try:
            chunk = next_chunk(config, project_root, fold_from=from_date.isoformat())
        except FileNotFoundError:
            log.warning("Queue file not found — stopping")
            break
        except ValueError:
            # Queue empty — done
            log.info("Queue exhausted after %d chunks", chunk_count)
            break

        chunk_count += 1
        log.info(
            "Processing chunk %d (%s, %d items, %s)",
            chunk.chunk_id,
            chunk.chunk_type,
            chunk.items_count,
            chunk.date_range or "drift triage",
        )

        success = _dispatch_and_validate(config, project_root, chunk)
        if success:
            log.info("Chunk %d committed", chunk.chunk_id)
        else:
            failures += 1
            log.error("Chunk %d failed", chunk.chunk_id)

    if failures:
        log.warning("Forward fold completed with %d failed chunk(s)", failures)
        return False

    db = ServerDB(project_root / ".engram" / "engram.db")
    db.clear_fold_from()
    log.info("Forward fold completed successfully (%d chunks)", chunk_count)
    return True
