"""Serial dispatch to fold agents with linting and retry.

Handles the dispatch lifecycle::

    building → dispatched → validated → committed

Shells out to a configurable fold agent CLI (``claude``, ``codex``, etc.),
runs the schema linter on results, and auto-retries with correction prompts
on failure (max 2).  L0 briefing regeneration is deferred to queue drain.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engram.config import resolve_doc_paths
from engram.dispatch import invoke_agent, read_docs
from engram.fold.chunker import ChunkResult, next_chunk
from engram.linter import LintResult, lint_post_dispatch

log = logging.getLogger(__name__)

MAX_RETRIES = 2


class Dispatcher:
    """Manages serial dispatch of fold chunks.

    Parameters
    ----------
    config:
        Engram config dict.
    project_root:
        Project root directory.
    db:
        ServerDB instance for dispatch state tracking.
    """

    def __init__(
        self,
        config: dict[str, Any],
        project_root: Path,
        db: Any,
    ) -> None:
        self._config = config
        self._project_root = project_root
        self._db = db

    def dispatch(self) -> bool:
        """Execute a single dispatch cycle.

        Builds a chunk via the chunker, dispatches to the fold agent,
        and validates with the linter.  On success, marks L0 stale
        (briefing regeneration deferred to queue drain).

        Returns True if dispatch succeeded, False on failure.
        """
        doc_paths = resolve_doc_paths(self._config, self._project_root)

        # Snapshot living docs before dispatch
        before_contents = read_docs(doc_paths, ("timeline", "concepts", "epistemic", "workflows"))

        buffered = self._flush_buffer_to_queue()
        if buffered:
            log.info("Flushed %d buffered item(s) into queue", buffered)

        # Build the chunk
        try:
            fold_from = self._db.get_fold_from()
            chunk = next_chunk(self._config, self._project_root, fold_from=fold_from)
        except (FileNotFoundError, ValueError) as exc:
            log.warning("Cannot build chunk: %s", exc)
            return False

        # Record dispatch in DB
        dispatch_id = self._db.create_dispatch(
            chunk_id=chunk.chunk_id,
            input_path=str(chunk.input_path),
            prompt_path=str(chunk.prompt_path),
        )

        # Transition to dispatched
        self._db.update_dispatch_state(dispatch_id, "dispatched")

        # Execute fold agent
        success = self._execute_and_validate(
            dispatch_id=dispatch_id,
            chunk=chunk,
            before_contents=before_contents,
            doc_paths=doc_paths,
        )

        if success:
            self._db.update_dispatch_state(dispatch_id, "validated")
            self._db.mark_l0_stale()  # stale BEFORE committed — crash-safe ordering
            self._db.update_dispatch_state(dispatch_id, "committed")
            self._db.update_server_state(
                last_dispatch_time=datetime.now(timezone.utc).isoformat(),
            )
            log.info("Dispatch %d (chunk %d) committed", dispatch_id, chunk.chunk_id)
            return True

        log.error("Dispatch %d (chunk %d) failed after retries", dispatch_id, chunk.chunk_id)
        return False

    def _execute_and_validate(
        self,
        dispatch_id: int,
        chunk: ChunkResult,
        before_contents: dict[str, str],
        doc_paths: dict[str, Path],
    ) -> bool:
        """Execute the fold agent and validate results. Retry on lint failure."""
        correction_text: str | None = None

        for attempt in range(1 + MAX_RETRIES):
            if attempt > 0:
                retry_count = self._db.increment_retry(dispatch_id)
                log.info("Retry %d/%d for dispatch %d", retry_count, MAX_RETRIES, dispatch_id)

            # Shell out to fold agent — on retry, include correction context
            prompt = chunk.prompt_path.read_text()
            if correction_text:
                prompt = prompt + "\n\n" + correction_text
            ok = invoke_agent(self._config, self._project_root, prompt)
            if not ok:
                self._db.update_dispatch_state(
                    dispatch_id, "dispatched", error="Agent invocation failed",
                )
                continue

            # Read after state
            after_contents = read_docs(doc_paths, ("timeline", "concepts", "epistemic", "workflows"))
            graveyard_docs = read_docs(doc_paths, ("concept_graveyard", "epistemic_graveyard"))

            # Flatten pre-assigned IDs for linter
            pre_assigned: list[str] = []
            for id_list in chunk.pre_assigned_ids.values():
                pre_assigned.extend(id_list)

            # Run linter
            result = lint_post_dispatch(
                before_contents=before_contents,
                after_contents=after_contents,
                graveyard_docs=graveyard_docs,
                pre_assigned_ids=pre_assigned if pre_assigned else None,
                expected_growth=chunk.chunk_chars,
                config=self._config,
                project_root=self._project_root,
            )

            if result.passed:
                return True

            # Lint failed — log violations and build correction for next attempt
            log.warning(
                "Lint failed (%d violations) for chunk %d:",
                len(result.violations), chunk.chunk_id,
            )
            for v in result.violations:
                log.warning("  [%s/%s] %s", v.doc_type, v.entry_id or "", v.message)

            correction_text = _build_correction_text(chunk, result)

            self._db.update_dispatch_state(
                dispatch_id, "dispatched",
                error=f"Lint failed: {len(result.violations)} violations",
            )

        return False

    def recover_dispatch(self, dispatch: dict[str, Any]) -> bool:
        """Recover a dispatch found in non-terminal state on startup.

        Recovery strategy per state:
        - ``validated``: mark L0 stale and committed (L0 regen deferred to drain).
        - ``dispatched``: Agent may have completed. Re-lint; if valid, proceed
          to mark L0 stale + committed. If lint fails and retries remain, re-dispatch.
        """
        doc_paths = resolve_doc_paths(self._config, self._project_root)
        dispatch_id = dispatch["id"]

        if dispatch["state"] == "validated":
            # mark_l0_stale BEFORE committed — crash-safe ordering
            self._db.mark_l0_stale()
            self._db.update_dispatch_state(dispatch_id, "committed")
            log.info("Recovered validated dispatch %d: marked L0 stale + committed", dispatch_id)
            return True

        if dispatch["state"] == "dispatched":
            input_path = Path(dispatch["input_path"]) if dispatch["input_path"] else None
            prompt_path = Path(dispatch["prompt_path"]) if dispatch.get("prompt_path") else None

            if input_path and input_path.exists():
                # Re-read docs and try to validate
                after_contents = read_docs(
                    doc_paths, ("timeline", "concepts", "epistemic", "workflows"),
                )
                graveyard_docs = read_docs(
                    doc_paths, ("concept_graveyard", "epistemic_graveyard"),
                )

                from engram.linter import lint
                result = lint(after_contents, graveyard_docs, self._config, doc_paths=doc_paths)

                if result.passed:
                    self._db.update_dispatch_state(dispatch_id, "validated")
                    self._db.mark_l0_stale()  # stale BEFORE committed
                    self._db.update_dispatch_state(dispatch_id, "committed")
                    log.info("Recovered dispatch %d as committed", dispatch_id)
                    return True

                # Lint failed — re-dispatch if retries remain
                if dispatch["retry_count"] < MAX_RETRIES and prompt_path and prompt_path.exists():
                    log.info(
                        "Recovery: lint failed for dispatch %d, re-dispatching (retry %d/%d)",
                        dispatch_id, dispatch["retry_count"] + 1, MAX_RETRIES,
                    )
                    retry_count = self._db.increment_retry(dispatch_id)

                    # Build a minimal ChunkResult for re-invocation
                    correction_text = _build_correction_text_from_lint(result)
                    ok = self._invoke_fold_agent_from_path(prompt_path, correction_text)
                    if ok:
                        # Re-lint after retry
                        after2 = read_docs(
                            doc_paths, ("timeline", "concepts", "epistemic", "workflows"),
                        )
                        graveyard2 = read_docs(
                            doc_paths, ("concept_graveyard", "epistemic_graveyard"),
                        )
                        result2 = lint(after2, graveyard2, self._config, doc_paths=doc_paths)
                        if result2.passed:
                            self._db.update_dispatch_state(dispatch_id, "validated")
                            self._db.mark_l0_stale()  # stale BEFORE committed
                            self._db.update_dispatch_state(dispatch_id, "committed")
                            log.info("Recovery re-dispatch succeeded for dispatch %d", dispatch_id)
                            return True

            # Cannot recover — mark committed with error
            self._db.update_dispatch_state(
                dispatch_id, "committed",
                error="Recovered: could not validate after retries",
            )
            log.warning("Recovered dispatch %d with error", dispatch_id)
            return False

        return False

    def _invoke_fold_agent_from_path(
        self,
        prompt_path: Path,
        correction_text: str | None = None,
    ) -> bool:
        """Re-invoke fold agent using a prompt file path (for recovery)."""
        prompt = prompt_path.read_text()
        if correction_text:
            prompt = prompt + "\n\n" + correction_text
        return invoke_agent(self._config, self._project_root, prompt)

    def _flush_buffer_to_queue(self) -> int:
        """Move pending buffer items into ``queue.jsonl`` for chunking."""
        items = self._db.get_buffer_items()
        if not items:
            return 0

        engram_dir = self._project_root / ".engram"
        engram_dir.mkdir(parents=True, exist_ok=True)
        queue_file = engram_dir / "queue.jsonl"

        queue: list[dict[str, Any]] = []
        if queue_file.exists():
            with open(queue_file) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        queue.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        existing_paths = {
            entry.get("path")
            for entry in queue
            if isinstance(entry.get("path"), str)
        }
        consume_ids: list[int] = []
        added = 0

        for item in items:
            entry = self._buffer_item_to_queue_entry(item)
            consume_ids.append(item["id"])
            if entry is None:
                continue
            if entry["path"] in existing_paths:
                continue
            queue.append(entry)
            existing_paths.add(entry["path"])
            added += 1

        queue.sort(key=lambda e: str(e.get("date", "")))
        with open(queue_file, "w") as fh:
            for entry in queue:
                fh.write(json.dumps(entry) + "\n")

        if consume_ids:
            self._db.consume_buffer(consume_ids)
        return added

    def _buffer_item_to_queue_entry(
        self,
        item: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Convert a buffered watcher item into queue.jsonl entry schema."""
        path = item.get("path")
        item_type = item.get("item_type")
        if not isinstance(path, str) or not isinstance(item_type, str):
            return None

        entry_date = item.get("date") or item.get("added_at")
        if not isinstance(entry_date, str) or not entry_date:
            entry_date = datetime.now(timezone.utc).isoformat()
        chars = int(item.get("chars") or 0)

        if item_type == "doc":
            return {
                "date": entry_date,
                "type": "doc",
                "path": path,
                "chars": chars,
                "pass": "revisit",
            }

        if item_type == "issue":
            issue_number, issue_title = self._resolve_issue_metadata(path)
            return {
                "date": entry_date,
                "type": "issue",
                "path": path,
                "chars": chars,
                "pass": "initial",
                "issue_number": issue_number,
                "issue_title": issue_title,
            }

        if item_type == "prompts":
            prompt_count = 1
            metadata = item.get("metadata")
            if isinstance(metadata, str) and metadata:
                try:
                    parsed = json.loads(metadata)
                    prompt_count = int(parsed.get("prompt_count", 1))
                except (json.JSONDecodeError, TypeError, ValueError):
                    prompt_count = 1

            return {
                "date": entry_date,
                "type": "prompts",
                "path": path,
                "chars": chars,
                "pass": "initial",
                "session_id": Path(path).stem,
                "prompt_count": max(prompt_count, 1),
            }

        return None

    def _resolve_issue_metadata(self, rel_path: str) -> tuple[int, str]:
        """Resolve issue number/title from issue JSON path when possible."""
        issue_number = 0
        issue_title = ""
        issue_path = self._project_root / rel_path

        if issue_path.exists():
            try:
                issue = json.loads(issue_path.read_text())
                raw_number = issue.get("number", 0)
                issue_number = int(raw_number)
                raw_title = issue.get("title", "")
                issue_title = str(raw_title) if raw_title is not None else ""
            except (json.JSONDecodeError, TypeError, ValueError, OSError):
                pass

        if issue_number == 0:
            match = re.match(r"(\d+)", Path(rel_path).stem)
            if match:
                issue_number = int(match.group(1))

        return issue_number, issue_title


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _build_correction_text_from_lint(result: LintResult) -> str:
    """Build correction text from a LintResult (for crash recovery)."""
    violations_text = "\n".join(
        f"- [{v.doc_type}/{v.entry_id or ''}] {v.message}"
        for v in result.violations
    )
    return (
        f"CORRECTION REQUIRED: The previous fold attempt had "
        f"{len(result.violations)} lint violations:\n\n"
        f"{violations_text}\n\n"
        f"Please fix these violations in the living docs.\n"
    )


def _build_correction_text(chunk: ChunkResult, result: LintResult) -> str:
    """Build correction context for a retry prompt.

    Returns text to append to the agent prompt so it sees the lint
    violations from the previous attempt.
    """
    violations_text = "\n".join(
        f"- [{v.doc_type}/{v.entry_id or ''}] {v.message}"
        for v in result.violations
    )
    return (
        f"CORRECTION REQUIRED: The previous fold attempt for chunk {chunk.chunk_id} had "
        f"{len(result.violations)} lint violations:\n\n"
        f"{violations_text}\n\n"
        f"Please fix these violations in the living docs. "
        f"Re-read the input file at {chunk.input_path.resolve()} for context.\n"
    )
