"""Session history adapters for ingesting user prompts.

Pluggable interface with built-in adapters:
- claude-code: parse ~/.claude/history.jsonl
- codex: stub (format TBD)
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

# Minimum prompt length to include (filters slash commands and trivial inputs)
MIN_PROMPT_CHARS = 25


class SessionEntry:
    """A single parsed session with rendered markdown content."""

    __slots__ = ("session_id", "date", "chars", "prompt_count", "rendered")

    def __init__(
        self,
        session_id: str,
        date: str,
        chars: int,
        prompt_count: int,
        rendered: str,
    ) -> None:
        self.session_id = session_id
        self.date = date
        self.chars = chars
        self.prompt_count = prompt_count
        self.rendered = rendered


class SessionAdapter(ABC):
    """Base class for session history adapters."""

    @abstractmethod
    def parse(
        self,
        path: Path,
        project_match: list[str],
    ) -> list[SessionEntry]:
        """Parse session history and return filtered entries.

        Args:
            path: Path to the history file.
            project_match: Substrings to match against project paths.
                Empty list means match all.

        Returns:
            List of SessionEntry, one per session.
        """


class ClaudeCodeAdapter(SessionAdapter):
    """Parse Claude Code's ~/.claude/history.jsonl.

    Groups prompts by session, filters by project_match, renders as markdown.
    """

    def parse(
        self,
        path: Path,
        project_match: list[str],
    ) -> list[SessionEntry]:
        if not path.exists():
            return []

        # Group prompts by session
        sessions: dict[str, list[dict[str, Any]]] = {}
        with open(path) as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Filter to matching projects
                project = entry.get("project", "").lower()
                if project_match and not any(
                    p.lower() in project for p in project_match
                ):
                    continue

                prompt = entry.get("display", "")

                # Skip slash commands and trivial inputs
                if prompt.startswith("/") or len(prompt) < MIN_PROMPT_CHARS:
                    continue

                session_id = entry.get("sessionId", "unknown")
                if session_id not in sessions:
                    sessions[session_id] = []
                sessions[session_id].append(entry)

        # Build one SessionEntry per session
        entries = []
        for session_id, prompts in sessions.items():
            if not prompts:
                continue

            rendered = _render_session_markdown(prompts)
            session_date = datetime.fromtimestamp(
                prompts[0]["timestamp"] / 1000
            ).isoformat()

            entries.append(SessionEntry(
                session_id=session_id,
                date=session_date,
                chars=len(rendered),
                prompt_count=len(prompts),
                rendered=rendered,
            ))

        return entries


class CodexAdapter(SessionAdapter):
    """Stub adapter for OpenAI Codex sessions (format TBD)."""

    def parse(
        self,
        path: Path,
        project_match: list[str],
    ) -> list[SessionEntry]:
        return []


# Registry of built-in adapters
ADAPTERS: dict[str, type[SessionAdapter]] = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}


def get_adapter(format_name: str) -> SessionAdapter:
    """Get an adapter instance by format name.

    Raises:
        ValueError: If format_name is not a known adapter.
    """
    cls = ADAPTERS.get(format_name)
    if cls is None:
        raise ValueError(
            f"Unknown session format '{format_name}'. "
            f"Available: {sorted(ADAPTERS.keys())}"
        )
    return cls()


def _render_session_markdown(prompts: list[dict[str, Any]]) -> str:
    """Render a list of prompts from one session as markdown."""
    lines = []
    for p in prompts:
        ts = datetime.fromtimestamp(p["timestamp"] / 1000)
        text = p["display"]
        lines.append(f"**[{ts.strftime('%H:%M')}]** {text}")
        lines.append("")
    return "\n".join(lines)
