"""Filesystem and polling watchers for the engram server.

Three event sources:

1. **Filesystem watcher** — ``watchdog`` observer on configured source dirs.
   Detects file creates/modifies in doc and issue directories.
2. **Git poller** — periodic ``git log --since`` to detect new commits.
3. **Session poller** — mtime check on session history file (e.g.
   ``~/.claude/history.jsonl``), filters new entries by ``project_match``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Types
# ------------------------------------------------------------------

# Callback signature: (path, item_type, chars, date, metadata)
BufferCallback = Callable[[str, str, int, str | None, str | None], None]


# ------------------------------------------------------------------
# Filesystem watcher (watchdog)
# ------------------------------------------------------------------


class _DocEventHandler(FileSystemEventHandler):
    """Watchdog handler that calls back on file create/modify events."""

    def __init__(
        self,
        callback: BufferCallback,
        project_root: Path,
        extensions: tuple[str, ...] = (".md", ".txt", ".json", ".yaml", ".yml"),
    ) -> None:
        super().__init__()
        self._callback = callback
        self._project_root = project_root
        self._extensions = extensions

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def _handle(self, abs_path: str) -> None:
        path = Path(abs_path)
        if path.suffix.lower() not in self._extensions:
            return
        # Skip hidden files and .engram directory
        rel = path.relative_to(self._project_root)
        parts = rel.parts
        if any(p.startswith(".") for p in parts):
            return

        try:
            chars = path.stat().st_size
        except OSError:
            chars = 0

        rel_str = str(rel)
        item_type = "issue" if path.suffix == ".json" else "doc"
        self._callback(rel_str, item_type, chars, None, None)


class FileWatcher:
    """Watchdog-based filesystem watcher for source directories.

    Parameters
    ----------
    config:
        Engram config dict.
    project_root:
        Project root directory.
    callback:
        Called when a relevant file event occurs.
    """

    def __init__(
        self,
        config: dict[str, Any],
        project_root: Path,
        callback: BufferCallback,
    ) -> None:
        self._config = config
        self._project_root = project_root
        self._callback = callback
        self._observer: Observer | None = None

    def start(self) -> None:
        """Start the filesystem observer."""
        sources = self._config.get("sources", {})
        watch_dirs: list[Path] = []

        # Doc directories
        for d in sources.get("docs", []):
            p = self._project_root / d
            if p.exists():
                watch_dirs.append(p)

        # Issues directory
        issues_dir = self._project_root / sources.get("issues", "")
        if issues_dir.exists():
            watch_dirs.append(issues_dir)

        if not watch_dirs:
            log.warning("No source directories found to watch")
            return

        handler = _DocEventHandler(self._callback, self._project_root)
        self._observer = Observer()
        for d in watch_dirs:
            self._observer.schedule(handler, str(d), recursive=True)
            log.info("Watching: %s", d)

        self._observer.daemon = True
        self._observer.start()

    def stop(self) -> None:
        """Stop the filesystem observer."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None


# ------------------------------------------------------------------
# Git poller
# ------------------------------------------------------------------


class GitPoller:
    """Polls ``git log --since`` to detect new commits.

    Parameters
    ----------
    project_root:
        Git repository root.
    callback:
        Called for each new commit detected.
    source_dirs:
        Directories to filter git changes to.
    """

    def __init__(
        self,
        project_root: Path,
        callback: BufferCallback,
        source_dirs: list[str] | None = None,
    ) -> None:
        self._project_root = project_root
        self._callback = callback
        self._source_dirs = source_dirs or []
        self._last_commit: str | None = None

    def set_last_commit(self, commit_hash: str | None) -> None:
        """Set the bookmark for the last known commit."""
        self._last_commit = commit_hash

    def get_last_commit(self) -> str | None:
        return self._last_commit

    def poll(self) -> list[str]:
        """Check for new commits since last poll.

        Returns list of new commit hashes found.
        """
        try:
            # Get current HEAD
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(self._project_root),
                timeout=10,
            )
            if result.returncode != 0:
                return []
            current_head = result.stdout.strip()

            if self._last_commit == current_head:
                return []

            # Get commits since last known
            if self._last_commit:
                cmd = [
                    "git", "log",
                    f"{self._last_commit}..HEAD",
                    "--format=%H",
                ]
            else:
                # First poll — just record current HEAD, don't backfill
                self._last_commit = current_head
                return []

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self._project_root),
                timeout=30,
            )
            if result.returncode != 0:
                return []

            new_commits = [
                h.strip() for h in result.stdout.strip().split("\n") if h.strip()
            ]

            if new_commits:
                # Save old bookmark for diff range before updating
                old_commit = self._last_commit
                self._last_commit = current_head

                # Get changed files between old bookmark and current HEAD
                diff_cmd = [
                    "git", "diff", "--name-only",
                    f"{old_commit}..{current_head}",
                ]
                diff_result = subprocess.run(
                    diff_cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(self._project_root),
                    timeout=30,
                )
                if diff_result.returncode == 0:
                    for changed_file in diff_result.stdout.strip().split("\n"):
                        changed_file = changed_file.strip()
                        if not changed_file:
                            continue
                        # Filter to source dirs if configured
                        if self._source_dirs:
                            if not any(changed_file.startswith(d) for d in self._source_dirs):
                                continue
                        file_path = self._project_root / changed_file
                        chars = 0
                        if file_path.exists():
                            try:
                                chars = file_path.stat().st_size
                            except OSError:
                                pass
                        self._callback(changed_file, "doc", chars, None, None)

            return new_commits

        except (subprocess.TimeoutExpired, FileNotFoundError):
            log.warning("Git polling failed")
            return []


# ------------------------------------------------------------------
# Session history poller
# ------------------------------------------------------------------


class SessionPoller:
    """Polls session history file for new entries.

    Watches the mtime of the sessions file (e.g. ``~/.claude/history.jsonl``)
    and when it changes, parses new entries filtered by ``project_match``.

    Parameters
    ----------
    config:
        Engram config dict (needs ``sources.sessions``).
    callback:
        Called for each new session detected.
    """

    def __init__(
        self,
        config: dict[str, Any],
        callback: BufferCallback,
        project_root: Path | None = None,
    ) -> None:
        self._config = config
        self._callback = callback
        self._project_root = project_root
        self._last_mtime: float | None = None
        self._last_offset: int = 0
        self._last_tree_mtime: float | None = None
        self._known_prompt_counts: dict[str, int] = {}

        sessions_cfg = config.get("sources", {}).get("sessions", {})
        path_str = sessions_cfg.get("path", "~/.claude/history.jsonl")
        self._path = Path(os.path.expanduser(path_str))
        self._project_match: list[str] = sessions_cfg.get("project_match", [])
        self._format: str = sessions_cfg.get("format", "claude-code")
        self._session_tree: Path | None = None
        if self._format == "codex":
            self._session_tree = self._path.parent / "sessions"

    def set_last_mtime(self, mtime: float | None) -> None:
        """Set the bookmark for last known mtime."""
        self._last_mtime = mtime

    def get_last_mtime(self) -> float | None:
        return self._last_mtime

    def set_last_offset(self, offset: int) -> None:
        """Set the bookmark for last processed byte offset."""
        self._last_offset = max(0, int(offset))

    def get_last_offset(self) -> int:
        return self._last_offset

    def set_last_tree_mtime(self, mtime: float | None) -> None:
        """Set bookmark for codex sessions tree mtime."""
        self._last_tree_mtime = mtime

    def get_last_tree_mtime(self) -> float | None:
        return self._last_tree_mtime

    def poll(self) -> int:
        """Check for new session entries.

        Returns count of new session entries added to buffer.
        """
        if not self._path.exists():
            return 0

        try:
            current_mtime = self._path.stat().st_mtime
            current_size = self._path.stat().st_size
        except OSError:
            return 0

        tree_mtime = _latest_tree_mtime(self._session_tree)

        history_changed = (
            self._last_mtime is None
            or current_mtime > self._last_mtime
            or current_size < self._last_offset
        )
        tree_changed = (
            tree_mtime is not None
            and (
                self._last_tree_mtime is None
                or tree_mtime > self._last_tree_mtime
            )
        )
        if not history_changed and not tree_changed:
            return 0

        # File changed — parse incremental session entries.
        from engram.fold.sessions import get_adapter

        try:
            adapter = get_adapter(self._format)
        except ValueError:
            log.warning("Unknown session format: %s", self._format)
            self._last_mtime = current_mtime
            self._last_offset = current_size
            self._last_tree_mtime = tree_mtime
            return 0

        force_full_reparse = (
            self._format == "codex"
            and tree_changed
            and not history_changed
            and bool(self._project_match)
        )
        start_offset = 0 if current_size < self._last_offset else self._last_offset
        if force_full_reparse:
            start_offset = 0
        entries, new_offset = adapter.parse_incremental(
            self._path,
            self._project_match,
            start_offset=start_offset,
        )

        count = 0
        for entry in entries:
            known_prompts = self._known_prompt_counts.get(entry.session_id, 0)
            emitted_prompt_count = entry.prompt_count
            if force_full_reparse:
                if entry.prompt_count <= known_prompts:
                    continue
                emitted_prompt_count = entry.prompt_count - known_prompts

            rel_path = f".engram/sessions/{entry.session_id}.md"
            chars = entry.chars
            if self._project_root:
                rel_path, chars = self._write_session_file(
                    session_id=entry.session_id,
                    rendered=entry.rendered,
                    reset=start_offset == 0,
                )
            self._callback(
                rel_path,
                "prompts",
                chars,
                entry.date,
                json.dumps({"prompt_count": emitted_prompt_count}),
            )
            if start_offset == 0:
                self._known_prompt_counts[entry.session_id] = entry.prompt_count
            else:
                self._known_prompt_counts[entry.session_id] = (
                    known_prompts + entry.prompt_count
                )
            count += 1

        self._last_mtime = current_mtime
        self._last_offset = new_offset
        self._last_tree_mtime = tree_mtime
        return count

    def _write_session_file(
        self,
        session_id: str,
        rendered: str,
        *,
        reset: bool,
    ) -> tuple[str, int]:
        """Write incremental session markdown under ``.engram/sessions``."""
        if not self._project_root:
            rel_path = f".engram/sessions/{session_id}.md"
            return rel_path, len(rendered)

        sessions_dir = self._project_root / ".engram" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_file = sessions_dir / f"{session_id}.md"

        if reset or not session_file.exists():
            session_file.write_text(rendered)
        else:
            with open(session_file, "a") as fh:
                if session_file.stat().st_size > 0:
                    fh.write("\n")
                fh.write(rendered)

        rel_path = str(session_file.relative_to(self._project_root))
        try:
            chars = session_file.stat().st_size
        except OSError:
            chars = len(rendered)
        return rel_path, chars


def _latest_tree_mtime(path: Path | None) -> float | None:
    """Return latest mtime under tree path, or None when unavailable."""
    if path is None or not path.exists():
        return None
    latest: float | None = None
    try:
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            mtime = child.stat().st_mtime
            if latest is None or mtime > latest:
                latest = mtime
    except OSError:
        return latest
    return latest
