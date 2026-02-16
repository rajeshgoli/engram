"""Serial dispatch to fold agents with linting and retry.

Handles the dispatch lifecycle::

    building → dispatched → validated → committed

Shells out to a configurable fold agent CLI (``claude``, ``codex``, etc.),
runs the schema linter on results, auto-retries with correction prompts
on failure (max 2), and regenerates the L0 briefing after success.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engram.config import resolve_doc_paths
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
        validates with the linter, retries on failure, and regenerates
        the L0 briefing on success.

        Returns True if dispatch succeeded, False on failure.
        """
        doc_paths = resolve_doc_paths(self._config, self._project_root)

        # Snapshot living docs before dispatch
        before_contents = _read_docs(doc_paths, ("timeline", "concepts", "epistemic", "workflows"))

        # Build the chunk
        try:
            chunk = next_chunk(self._config, self._project_root)
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

            # Regenerate L0 briefing
            self._regenerate_l0_briefing(doc_paths)
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
        for attempt in range(1 + MAX_RETRIES):
            if attempt > 0:
                retry_count = self._db.increment_retry(dispatch_id)
                log.info("Retry %d/%d for dispatch %d", retry_count, MAX_RETRIES, dispatch_id)

            # Shell out to fold agent
            ok = self._invoke_fold_agent(chunk)
            if not ok:
                self._db.update_dispatch_state(
                    dispatch_id, "dispatched", error="Agent invocation failed",
                )
                continue

            # Read after state
            after_contents = _read_docs(doc_paths, ("timeline", "concepts", "epistemic", "workflows"))
            graveyard_docs = _read_docs(doc_paths, ("concept_graveyard", "epistemic_graveyard"))

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
            )

            if result.passed:
                return True

            # Lint failed — log violations
            log.warning(
                "Lint failed (%d violations) for chunk %d:",
                len(result.violations), chunk.chunk_id,
            )
            for v in result.violations:
                log.warning("  [%s/%s] %s", v.doc_type, v.entry_id or "", v.message)

            if attempt < MAX_RETRIES:
                # Write correction prompt for retry
                self._write_correction_prompt(chunk, result)

            self._db.update_dispatch_state(
                dispatch_id, "dispatched",
                error=f"Lint failed: {len(result.violations)} violations",
            )

        return False

    def _invoke_fold_agent(self, chunk: ChunkResult) -> bool:
        """Shell out to the configured fold agent CLI.

        Returns True if the agent completed successfully.
        """
        model = self._config.get("model", "sonnet")

        # Build agent command — configurable via config
        agent_cmd = self._config.get("agent_command")
        if agent_cmd:
            cmd = agent_cmd.split()
        else:
            cmd = ["claude", "--print", "--model", model]

        prompt = chunk.prompt_path.read_text()
        cmd.append(prompt)

        log.info("Invoking fold agent: %s", " ".join(cmd[:3]) + " ...")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self._project_root),
                timeout=600,  # 10 min timeout
            )
            if result.returncode != 0:
                log.error("Fold agent failed (rc=%d): %s", result.returncode, result.stderr[:500])
                return False
            return True
        except subprocess.TimeoutExpired:
            log.error("Fold agent timed out (10 min)")
            return False
        except FileNotFoundError:
            log.error("Fold agent command not found: %s", cmd[0])
            return False

    def _write_correction_prompt(self, chunk: ChunkResult, result: LintResult) -> None:
        """Write a correction prompt file for retry."""
        violations_text = "\n".join(
            f"- [{v.doc_type}/{v.entry_id or ''}] {v.message}"
            for v in result.violations
        )
        correction = (
            f"The previous fold attempt for chunk {chunk.chunk_id} had "
            f"{len(result.violations)} lint violations:\n\n"
            f"{violations_text}\n\n"
            f"Please fix these violations in the living docs. "
            f"Re-read the input file at {chunk.input_path.resolve()} for context.\n"
        )
        correction_path = chunk.prompt_path.with_suffix(".correction.txt")
        correction_path.write_text(correction)
        log.info("Wrote correction prompt: %s", correction_path)

    def _regenerate_l0_briefing(self, doc_paths: dict[str, Path]) -> None:
        """Regenerate the L0 briefing section in the project's CLAUDE.md.

        Uses a lightweight model call to compress living docs into a
        concise briefing (~50-100 lines).
        """
        briefing_cfg = self._config.get("briefing", {})
        target_file = self._project_root / briefing_cfg.get("file", "CLAUDE.md")
        section_header = briefing_cfg.get("section", "## Project Knowledge Briefing")

        if not target_file.exists():
            log.warning("Briefing target file not found: %s", target_file)
            return

        # Read current living docs for briefing generation
        living_contents: list[str] = []
        for key in ("timeline", "concepts", "epistemic", "workflows"):
            p = doc_paths.get(key)
            if p and p.exists():
                content = p.read_text()
                # Truncate very large docs for briefing generation
                if len(content) > 10_000:
                    content = content[:10_000] + "\n\n[... truncated for briefing ...]\n"
                living_contents.append(f"### {key.title()}\n{content}")

        if not living_contents:
            return

        # Generate briefing via lightweight model call
        briefing_text = self._generate_briefing("\n\n".join(living_contents))
        if not briefing_text:
            log.warning("L0 briefing generation returned empty result")
            return

        # Inject into target file
        _inject_section(target_file, section_header, briefing_text)
        log.info("L0 briefing regenerated in %s", target_file)

    def _generate_briefing(self, living_docs_content: str) -> str | None:
        """Generate L0 briefing by shelling out to a fast model.

        Returns the briefing text, or None on failure.
        """
        prompt = (
            "Compress the following project knowledge into a concise briefing "
            "(50-100 lines). Focus on: what's alive vs dead, contested claims, "
            "key workflows, and agent guidance. Use stable IDs (C###/E###/W###).\n\n"
            f"{living_docs_content}"
        )

        try:
            result = subprocess.run(
                ["claude", "--print", "--model", "haiku", prompt],
                capture_output=True,
                text=True,
                cwd=str(self._project_root),
                timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            log.warning("L0 briefing generation failed")

        return None

    def recover_dispatch(self, dispatch: dict[str, Any]) -> bool:
        """Recover a dispatch found in non-terminal state on startup.

        For 'dispatched' state: check if living docs changed (agent may
        have completed), re-lint if so, otherwise re-dispatch.
        """
        doc_paths = resolve_doc_paths(self._config, self._project_root)
        dispatch_id = dispatch["id"]

        if dispatch["state"] == "dispatched":
            # Check if agent completed by looking for doc changes
            input_path = Path(dispatch["input_path"]) if dispatch["input_path"] else None

            if input_path and input_path.exists():
                # Re-read docs and try to validate
                after_contents = _read_docs(
                    doc_paths, ("timeline", "concepts", "epistemic", "workflows"),
                )
                graveyard_docs = _read_docs(
                    doc_paths, ("concept_graveyard", "epistemic_graveyard"),
                )

                from engram.linter import lint
                result = lint(after_contents, graveyard_docs, self._config)

                if result.passed:
                    self._db.update_dispatch_state(dispatch_id, "validated")
                    self._regenerate_l0_briefing(doc_paths)
                    self._db.update_dispatch_state(dispatch_id, "committed")
                    log.info("Recovered dispatch %d as committed", dispatch_id)
                    return True

            # Cannot validate — mark as failed
            self._db.update_dispatch_state(
                dispatch_id, "committed",
                error="Recovered: could not validate, marked committed with error",
            )
            log.warning("Recovered dispatch %d with error", dispatch_id)
            return False

        return False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _read_docs(doc_paths: dict[str, Path], keys: tuple[str, ...]) -> dict[str, str]:
    """Read document contents for the given keys."""
    contents: dict[str, str] = {}
    for key in keys:
        p = doc_paths.get(key)
        if p and p.exists():
            contents[key] = p.read_text()
        else:
            contents[key] = ""
    return contents


def _inject_section(file_path: Path, section_header: str, content: str) -> None:
    """Inject or replace a section in a file.

    Finds ``section_header`` and replaces everything until the next
    same-level heading (or EOF) with ``content``.
    """
    text = file_path.read_text()
    header_level = section_header.count("#")

    start = text.find(section_header)
    if start == -1:
        # Append section at end
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{section_header}\n\n{content}\n"
    else:
        # Find the end of this section (next same-level or higher heading)
        section_start = start + len(section_header)
        rest = text[section_start:]
        end_offset = len(rest)

        for i, line in enumerate(rest.split("\n")):
            if i == 0:
                continue
            stripped = line.lstrip()
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                if level <= header_level:
                    end_offset = sum(
                        len(l) + 1 for l in rest.split("\n")[:i]
                    )
                    break

        text = text[:start] + f"{section_header}\n\n{content}\n" + text[section_start + end_offset:]

    file_path.write_text(text)
