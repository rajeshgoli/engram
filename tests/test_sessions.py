"""Tests for engram.fold.sessions."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from engram.fold.sessions import (
    ClaudeCodeAdapter,
    CodexAdapter,
    SessionEntry,
    get_adapter,
    _render_session_markdown,
)


@pytest.fixture
def history_file(tmp_path: Path) -> Path:
    """Create a sample history.jsonl."""
    now_ms = int(time.time() * 1000)
    entries = [
        {
            "sessionId": "sess-001",
            "project": "/Users/dev/my-project",
            "display": "Implement the login feature with OAuth",
            "timestamp": now_ms - 3600_000,  # 1 hour ago
        },
        {
            "sessionId": "sess-001",
            "project": "/Users/dev/my-project",
            "display": "Add tests for the OAuth login flow",
            "timestamp": now_ms - 3000_000,
        },
        {
            "sessionId": "sess-002",
            "project": "/Users/dev/other-project",
            "display": "Fix the database connection pooling issue",
            "timestamp": now_ms - 1800_000,
        },
        {
            "sessionId": "sess-003",
            "project": "/Users/dev/my-project",
            "display": "/help",  # slash command, should be filtered
            "timestamp": now_ms - 900_000,
        },
        {
            "sessionId": "sess-004",
            "project": "/Users/dev/my-project",
            "display": "hi",  # too short, should be filtered
            "timestamp": now_ms - 600_000,
        },
    ]
    path = tmp_path / "history.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


class TestClaudeCodeAdapter:
    def test_groups_by_session(self, history_file: Path) -> None:
        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(history_file, project_match=["my-project"])

        # Should get sess-001 (2 prompts), sess-003 and sess-004 are filtered
        assert len(entries) == 1
        entry = entries[0]
        assert entry.session_id == "sess-001"
        assert entry.prompt_count == 2
        assert "OAuth" in entry.rendered

    def test_filters_by_project_match(self, history_file: Path) -> None:
        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(history_file, project_match=["other-project"])

        assert len(entries) == 1
        assert entries[0].session_id == "sess-002"
        assert entries[0].prompt_count == 1

    def test_empty_project_match_returns_all(self, history_file: Path) -> None:
        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(history_file, project_match=[])

        # All sessions with valid prompts: sess-001 (2), sess-002 (1)
        session_ids = {e.session_id for e in entries}
        assert "sess-001" in session_ids
        assert "sess-002" in session_ids

    def test_filters_slash_commands(self, history_file: Path) -> None:
        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(history_file, project_match=["my-project"])

        # sess-003 only has "/help" → filtered out
        session_ids = {e.session_id for e in entries}
        assert "sess-003" not in session_ids

    def test_filters_short_prompts(self, history_file: Path) -> None:
        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(history_file, project_match=["my-project"])

        session_ids = {e.session_id for e in entries}
        assert "sess-004" not in session_ids

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(tmp_path / "nope.jsonl", project_match=[])
        assert entries == []

    def test_malformed_json_lines_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "history.jsonl"
        now_ms = int(time.time() * 1000)
        with open(path, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps({
                "sessionId": "s1",
                "project": "/dev/proj",
                "display": "A valid prompt that is long enough to pass",
                "timestamp": now_ms,
            }) + "\n")

        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(path, project_match=[])
        assert len(entries) == 1

    def test_case_insensitive_project_match(self, history_file: Path) -> None:
        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(history_file, project_match=["My-Project"])
        assert len(entries) == 1
        assert entries[0].session_id == "sess-001"

    def test_session_date_from_first_prompt(self, history_file: Path) -> None:
        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(history_file, project_match=["my-project"])
        assert len(entries) == 1
        # Date should be parseable and represent the first prompt's timestamp
        assert entries[0].date  # non-empty


class TestCodexAdapter:
    def test_returns_empty(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        entries = adapter.parse(tmp_path / "codex.jsonl", project_match=[])
        assert entries == []


class TestGetAdapter:
    def test_claude_code(self) -> None:
        adapter = get_adapter("claude-code")
        assert isinstance(adapter, ClaudeCodeAdapter)

    def test_codex(self) -> None:
        adapter = get_adapter("codex")
        assert isinstance(adapter, CodexAdapter)

    def test_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="Unknown session format"):
            get_adapter("vscode")


class TestRenderSessionMarkdown:
    def test_renders_prompts(self) -> None:
        now_ms = int(time.time() * 1000)
        prompts = [
            {"display": "First prompt text here", "timestamp": now_ms},
            {"display": "Second prompt text here", "timestamp": now_ms + 60_000},
        ]
        result = _render_session_markdown(prompts)
        assert "First prompt text here" in result
        assert "Second prompt text here" in result
        assert "**[" in result  # timestamp prefix


class TestSessionEntry:
    def test_fields(self) -> None:
        entry = SessionEntry(
            session_id="s1",
            date="2026-01-01T00:00:00",
            chars=100,
            prompt_count=3,
            rendered="content",
        )
        assert entry.session_id == "s1"
        assert entry.chars == 100
        assert entry.prompt_count == 3
