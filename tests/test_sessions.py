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


@pytest.fixture
def codex_history_file(tmp_path: Path) -> Path:
    """Create a codex home with history and per-session metadata logs."""
    codex_home = tmp_path / ".codex"
    sessions_dir = codex_home / "sessions" / "2026" / "02" / "21"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    history = codex_home / "history.jsonl"
    history_entries = [
        {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "ts": 1771641000,
            "text": "Implement Codex ingestion with project filtering support",
        },
        {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "ts": 1771641060,
            "text": "Add tests for codex session adapter parsing behavior",
        },
        {
            "session_id": "22222222-2222-2222-2222-222222222222",
            "ts": 1771641120,
            "text": "Investigate unrelated repository prompt history entry",
        },
    ]
    with open(history, "w") as fh:
        for row in history_entries:
            fh.write(json.dumps(row) + "\n")

    session_1 = sessions_dir / (
        "rollout-2026-02-21T10-00-00-11111111-1111-1111-1111-111111111111.jsonl"
    )
    session_2 = sessions_dir / (
        "rollout-2026-02-21T10-05-00-22222222-2222-2222-2222-222222222222.jsonl"
    )
    session_1.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": "11111111-1111-1111-1111-111111111111",
            "cwd": "/Users/dev/my-project",
        },
    }) + "\n")
    session_2.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": "22222222-2222-2222-2222-222222222222",
            "cwd": "/Users/dev/other-project",
        },
    }) + "\n")

    return history


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

    def test_incremental_only_parses_appended_lines(self, history_file: Path) -> None:
        adapter = ClaudeCodeAdapter()
        first_entries, offset = adapter.parse_incremental(
            history_file, project_match=["my-project"], start_offset=0,
        )
        assert len(first_entries) == 1
        assert offset > 0

        with open(history_file, "a") as fh:
            fh.write(json.dumps({
                "sessionId": "sess-002",
                "project": "/Users/dev/other-project",
                "display": "A newly appended prompt that should be emitted once",
                "timestamp": int(time.time() * 1000),
            }) + "\n")

        second_entries, second_offset = adapter.parse_incremental(
            history_file, project_match=["other-project"], start_offset=offset,
        )
        assert len(second_entries) == 1
        assert second_entries[0].session_id == "sess-002"
        assert second_offset > offset

    def test_filters_sm_telemetry_and_dedupes_consecutive_prompts(self, tmp_path: Path) -> None:
        now_ms = int(time.time() * 1000)
        path = tmp_path / "history.jsonl"
        with open(path, "w") as fh:
            fh.write(json.dumps({
                "sessionId": "s1",
                "project": "/Users/dev/my-project",
                "display": "[sm wait] worker idle for 500s and still waiting",
                "timestamp": now_ms,
            }) + "\n")
            fh.write(json.dumps({
                "sessionId": "s1",
                "project": "/Users/dev/my-project",
                "display": "Real decision text that should be preserved",
                "timestamp": now_ms + 1,
            }) + "\n")
            fh.write(json.dumps({
                "sessionId": "s1",
                "project": "/Users/dev/my-project",
                "display": "Real decision text that should be preserved",
                "timestamp": now_ms + 2,
            }) + "\n")

        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(path, project_match=["my-project"])
        assert len(entries) == 1
        rendered = entries[0].rendered
        assert "[sm wait]" not in rendered
        assert rendered.count("Real decision text that should be preserved") == 1
        assert entries[0].prompt_count == 1

    def test_trims_long_relay_prompts(self, tmp_path: Path) -> None:
        now_ms = int(time.time() * 1000)
        path = tmp_path / "history.jsonl"
        relay = "[Input from: architect] " + ("x" * 600)
        with open(path, "w") as fh:
            fh.write(json.dumps({
                "sessionId": "s1",
                "project": "/Users/dev/my-project",
                "display": relay,
                "timestamp": now_ms,
            }) + "\n")

        adapter = ClaudeCodeAdapter()
        entries = adapter.parse(path, project_match=["my-project"])
        assert len(entries) == 1
        rendered = entries[0].rendered
        assert "[Input from: architect]" in rendered
        assert "..." in rendered


class TestCodexAdapter:
    def test_nonexistent_file(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        entries = adapter.parse(tmp_path / "codex.jsonl", project_match=[])
        assert entries == []

    def test_parses_and_groups_codex_prompts(self, codex_history_file: Path) -> None:
        adapter = CodexAdapter()
        entries = adapter.parse(codex_history_file, project_match=[])
        assert len(entries) == 2
        by_session = {e.session_id: e for e in entries}
        assert by_session["11111111-1111-1111-1111-111111111111"].prompt_count == 2
        assert "project filtering support" in by_session[
            "11111111-1111-1111-1111-111111111111"
        ].rendered

    def test_filters_using_codex_session_cwd(self, codex_history_file: Path) -> None:
        adapter = CodexAdapter()
        entries = adapter.parse(codex_history_file, project_match=["my-project"])
        assert len(entries) == 1
        assert entries[0].session_id == "11111111-1111-1111-1111-111111111111"

    def test_incremental_reads_only_new_codex_lines(self, codex_history_file: Path) -> None:
        adapter = CodexAdapter()
        first, offset = adapter.parse_incremental(
            codex_history_file, project_match=["my-project"], start_offset=0,
        )
        assert len(first) == 1
        assert offset > 0

        with open(codex_history_file, "a") as fh:
            fh.write(json.dumps({
                "session_id": "11111111-1111-1111-1111-111111111111",
                "ts": 1771641180,
                "text": "Fresh appended codex prompt for incremental polling test",
            }) + "\n")

        second, second_offset = adapter.parse_incremental(
            codex_history_file, project_match=["my-project"], start_offset=offset,
        )
        assert len(second) == 1
        assert second[0].prompt_count == 1
        assert second_offset > offset


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
