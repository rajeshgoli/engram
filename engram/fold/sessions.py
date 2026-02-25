"""Session history adapters for ingesting user prompts.

Pluggable interface with built-in adapters:
- claude-code: parse ~/.claude/history.jsonl
- codex: parse ~/.codex/history.jsonl with project matching from session logs
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Minimum prompt length to include (filters slash commands and trivial inputs)
MIN_PROMPT_CHARS = 25
_SM_TELEMETRY_RE = re.compile(r"^\[sm[^\]]*\]", re.IGNORECASE)
_RELAY_RE = re.compile(r"^\[input from:[^\]]+\]", re.IGNORECASE)
_RELAY_MAX_CHARS = 320


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

    def parse_incremental(
        self,
        path: Path,
        project_match: list[str],
        start_offset: int = 0,
    ) -> tuple[list[SessionEntry], int]:
        """Parse only entries appended after ``start_offset``.

        Returns:
            Tuple of (entries, new_offset).
        """
        entries = self.parse(path, project_match)
        try:
            new_offset = path.stat().st_size if path.exists() else start_offset
        except OSError:
            new_offset = start_offset
        return entries, new_offset


class ClaudeCodeAdapter(SessionAdapter):
    """Parse Claude Code's ~/.claude/history.jsonl.

    Groups prompts by session, filters by project_match, renders as markdown.
    """

    def parse(
        self,
        path: Path,
        project_match: list[str],
    ) -> list[SessionEntry]:
        entries, _ = self.parse_incremental(path, project_match, start_offset=0)
        return entries

    def parse_incremental(
        self,
        path: Path,
        project_match: list[str],
        start_offset: int = 0,
    ) -> tuple[list[SessionEntry], int]:
        if not path.exists():
            return [], start_offset

        sessions: dict[str, list[dict[str, Any]]] = {}
        try:
            size = path.stat().st_size
        except OSError:
            return [], start_offset

        if start_offset < 0 or start_offset > size:
            start_offset = 0

        with open(path) as fh:
            fh.seek(start_offset)
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
            new_offset = fh.tell()

        return _build_session_entries(sessions), new_offset


class CodexAdapter(SessionAdapter):
    """Parse Codex CLI history with project matching via session logs."""

    def parse(
        self,
        path: Path,
        project_match: list[str],
    ) -> list[SessionEntry]:
        entries, _ = self.parse_incremental(path, project_match, start_offset=0)
        return entries

    def parse_incremental(
        self,
        path: Path,
        project_match: list[str],
        start_offset: int = 0,
    ) -> tuple[list[SessionEntry], int]:
        if not path.exists():
            return [], start_offset

        try:
            size = path.stat().st_size
        except OSError:
            return [], start_offset

        if start_offset < 0 or start_offset > size:
            start_offset = 0

        sessions: dict[str, list[dict[str, Any]]] = {}
        with open(path) as fh:
            fh.seek(start_offset)
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                session_id = entry.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    continue

                text = entry.get("text", "")
                if not isinstance(text, str):
                    continue
                text = text.strip()
                if not text:
                    continue
                if text.startswith("/") or len(text) < MIN_PROMPT_CHARS:
                    continue

                timestamp_ms = _codex_ts_to_ms(entry.get("ts"))
                if timestamp_ms is None:
                    continue

                if session_id not in sessions:
                    sessions[session_id] = []
                sessions[session_id].append({
                    "display": text,
                    "timestamp": timestamp_ms,
                })
            new_offset = fh.tell()

        if project_match and sessions:
            codex_home = path.parent
            cwd_by_session = _load_codex_session_cwds(
                codex_home / "sessions",
                session_ids=set(sessions.keys()),
            )
            filtered: dict[str, list[dict[str, Any]]] = {}
            patterns = [p.lower() for p in project_match]
            for session_id, prompts in sessions.items():
                cwds = cwd_by_session.get(session_id, set())
                if not cwds:
                    continue
                if any(
                    any(pattern in cwd for pattern in patterns)
                    for cwd in cwds
                ):
                    filtered[session_id] = prompts
            sessions = filtered

        return _build_session_entries(sessions), new_offset


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
        ts = datetime.fromtimestamp(p["timestamp"] / 1000, tz=timezone.utc)
        text = p["display"]
        lines.append(f"**[{ts.strftime('%H:%M')}]** {text}")
        lines.append("")
    return "\n".join(lines)


def _build_session_entries(
    sessions: dict[str, list[dict[str, Any]]],
) -> list[SessionEntry]:
    """Build ``SessionEntry`` objects from grouped prompt dicts."""
    entries: list[SessionEntry] = []
    for session_id, prompts in sessions.items():
        if not prompts:
            continue
        prompts.sort(key=lambda p: p.get("timestamp", 0))
        filtered_prompts: list[dict[str, Any]] = []
        last_text: str | None = None
        for prompt in prompts:
            text_raw = prompt.get("display", "")
            if not isinstance(text_raw, str):
                continue
            normalized = _normalize_prompt_text(text_raw)
            if not normalized:
                continue
            if normalized == last_text:
                continue
            filtered_prompts.append({**prompt, "display": normalized})
            last_text = normalized

        if not filtered_prompts:
            continue

        rendered = _render_session_markdown(filtered_prompts)
        first_ts = filtered_prompts[0].get("timestamp", 0)
        session_date = datetime.fromtimestamp(
            first_ts / 1000, tz=timezone.utc,
        ).isoformat()
        entries.append(SessionEntry(
            session_id=session_id,
            date=session_date,
            chars=len(rendered),
            prompt_count=len(filtered_prompts),
            rendered=rendered,
        ))
    return entries


def _normalize_prompt_text(text: str) -> str:
    """Normalize session prompt text for fold consumption.

    - Drops pure ``[sm ...]`` telemetry lines.
    - Collapses multiline prompts to one line.
    - Trims long relay blocks (``[Input from: ...]``).
    """
    normalized = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if not normalized:
        return ""
    if _SM_TELEMETRY_RE.match(normalized):
        return ""
    if _RELAY_RE.match(normalized) and len(normalized) > _RELAY_MAX_CHARS:
        clipped = normalized[: _RELAY_MAX_CHARS - 3]
        clipped = clipped.rsplit(" ", 1)[0]
        return clipped + "..."
    return normalized


def _codex_ts_to_ms(raw: Any) -> int | None:
    """Normalize Codex ``ts`` values to epoch milliseconds."""
    if raw is None:
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None

    # Codex history uses epoch seconds today; tolerate ms inputs too.
    if ts >= 10_000_000_000:
        return int(ts)
    return int(ts * 1000)


def _load_codex_session_cwds(
    sessions_root: Path,
    session_ids: set[str],
) -> dict[str, set[str]]:
    """Map session_id -> observed cwd values from Codex session logs."""
    if not sessions_root.exists() or not session_ids:
        return {}

    out: dict[str, set[str]] = {}
    for session_file in sessions_root.rglob("*.jsonl"):
        sid_from_name = _session_id_from_name(session_file.name)
        if sid_from_name and sid_from_name not in session_ids:
            continue

        current_sid = sid_from_name
        try:
            with open(session_file) as fh:
                for line in fh:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")
                    payload = event.get("payload", {})
                    if not isinstance(payload, dict):
                        continue

                    if event_type == "session_meta":
                        sid = payload.get("id")
                        if isinstance(sid, str) and sid:
                            current_sid = sid
                        cwd = payload.get("cwd")
                        if _record_cwd(out, current_sid, cwd):
                            continue

                    if event_type == "turn_context":
                        cwd = payload.get("cwd")
                        if _record_cwd(out, current_sid, cwd):
                            continue

                    if event_type == "response_item":
                        cwd = _cwd_from_response_item(payload)
                        _record_cwd(out, current_sid, cwd)
        except OSError:
            continue

    # Keep only requested IDs.
    return {
        sid: cwds for sid, cwds in out.items()
        if sid in session_ids and cwds
    }


def _record_cwd(
    out: dict[str, set[str]],
    session_id: str | None,
    cwd: Any,
) -> bool:
    """Record cwd for session_id, returning True if it was added."""
    if not isinstance(session_id, str) or not session_id:
        return False
    if not isinstance(cwd, str) or not cwd:
        return False
    out.setdefault(session_id, set()).add(cwd.lower())
    return True


def _cwd_from_response_item(payload: dict[str, Any]) -> str | None:
    """Best-effort cwd extraction from a response_item message payload."""
    if payload.get("type") != "message":
        return None
    content = payload.get("content", [])
    if not isinstance(content, list):
        return None
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if not isinstance(text, str):
            continue
        match = re.search(r"<cwd>([^<]+)</cwd>", text)
        if match:
            return match.group(1).strip()
    return None


def _session_id_from_name(filename: str) -> str | None:
    """Extract UUID-like session ID from Codex session filename."""
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        filename,
    )
    if not match:
        return None
    return match.group(1)
