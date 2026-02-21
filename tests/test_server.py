"""Tests for the engram server module."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engram.server.db import DISPATCH_STATES, TERMINAL_STATES, ServerDB


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / ".engram" / "engram.db"


@pytest.fixture()
def db(db_path: Path) -> ServerDB:
    return ServerDB(db_path)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Set up a minimal engram project."""
    engram_dir = tmp_path / ".engram"
    engram_dir.mkdir()

    config_yaml = """\
living_docs:
  timeline: docs/decisions/timeline.md
  concepts: docs/decisions/concept_registry.md
  epistemic: docs/decisions/epistemic_state.md
  workflows: docs/decisions/workflow_registry.md
graveyard:
  concepts: docs/decisions/concept_graveyard.md
  epistemic: docs/decisions/epistemic_graveyard.md
thresholds:
  orphan_triage: 50
  contested_review_days: 14
  stale_unverified_days: 30
  workflow_repetition: 3
budget:
  context_limit_chars: 600000
  instructions_overhead: 10000
  max_chunk_chars: 200000
"""
    (engram_dir / "config.yaml").write_text(config_yaml)

    docs_dir = tmp_path / "docs" / "decisions"
    docs_dir.mkdir(parents=True)
    (docs_dir / "timeline.md").write_text("# Timeline\n")
    (docs_dir / "concept_registry.md").write_text("# Concept Registry\n")
    (docs_dir / "epistemic_state.md").write_text("# Epistemic State\n")
    (docs_dir / "workflow_registry.md").write_text("# Workflow Registry\n")
    (docs_dir / "concept_graveyard.md").write_text("# Concept Graveyard\n")
    (docs_dir / "epistemic_graveyard.md").write_text("# Epistemic Graveyard\n")

    # Create source dirs
    working_dir = tmp_path / "docs" / "working"
    working_dir.mkdir(parents=True, exist_ok=True)

    return tmp_path


@pytest.fixture()
def config(project: Path) -> dict:
    from engram.config import load_config
    return load_config(project)


# ==================================================================
# ServerDB Tests
# ==================================================================


class TestServerDBInit:
    def test_creates_tables(self, db_path: Path) -> None:
        db = ServerDB(db_path)
        conn = sqlite3.connect(str(db_path))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "buffer_items" in tables
        assert "dispatches" in tables
        assert "server_state" in tables

    def test_server_state_singleton(self, db: ServerDB) -> None:
        state = db.get_server_state()
        assert state["buffer_chars_total"] == 0

    def test_reinit_preserves_data(self, db_path: Path) -> None:
        db1 = ServerDB(db_path)
        db1.add_buffer_item("test.md", "doc", 100)
        # Re-init — data should persist
        db2 = ServerDB(db_path)
        items = db2.get_buffer_items()
        assert len(items) == 1

    def test_does_not_touch_id_counters(self, db_path: Path) -> None:
        """Verify db.py does not create id_counters table."""
        db = ServerDB(db_path)
        conn = sqlite3.connect(str(db_path))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "id_counters" not in tables


class TestBufferItems:
    def test_add_and_get(self, db: ServerDB) -> None:
        row_id = db.add_buffer_item("docs/spec.md", "doc", 500, "2025-01-15")
        assert row_id > 0

        items = db.get_buffer_items()
        assert len(items) == 1
        assert items[0]["path"] == "docs/spec.md"
        assert items[0]["item_type"] == "doc"
        assert items[0]["chars"] == 500

    def test_buffer_chars_tracked(self, db: ServerDB) -> None:
        db.add_buffer_item("a.md", "doc", 100)
        db.add_buffer_item("b.md", "doc", 200)
        assert db.get_buffer_chars() == 300

    def test_clear_buffer(self, db: ServerDB) -> None:
        db.add_buffer_item("a.md", "doc", 100)
        db.add_buffer_item("b.md", "doc", 200)
        count = db.clear_buffer()
        assert count == 2
        assert db.get_buffer_items() == []
        assert db.get_buffer_chars() == 0

    def test_consume_buffer(self, db: ServerDB) -> None:
        id1 = db.add_buffer_item("a.md", "doc", 100)
        id2 = db.add_buffer_item("b.md", "doc", 200)
        id3 = db.add_buffer_item("c.md", "doc", 300)

        consumed = db.consume_buffer([id1, id3])
        assert len(consumed) == 2
        assert {c["path"] for c in consumed} == {"a.md", "c.md"}
        assert db.get_buffer_chars() == 200

        remaining = db.get_buffer_items()
        assert len(remaining) == 1
        assert remaining[0]["path"] == "b.md"

    def test_consume_empty_list(self, db: ServerDB) -> None:
        assert db.consume_buffer([]) == []

    def test_has_buffer_item(self, db: ServerDB) -> None:
        assert not db.has_buffer_item("test.md")
        db.add_buffer_item("test.md", "doc", 50)
        assert db.has_buffer_item("test.md")

    def test_items_ordered_by_date(self, db: ServerDB) -> None:
        db.add_buffer_item("c.md", "doc", 100, "2025-03-01")
        db.add_buffer_item("a.md", "doc", 100, "2025-01-01")
        db.add_buffer_item("b.md", "doc", 100, "2025-02-01")
        items = db.get_buffer_items()
        assert [i["path"] for i in items] == ["a.md", "b.md", "c.md"]


class TestDispatches:
    def test_create_dispatch(self, db: ServerDB) -> None:
        did = db.create_dispatch(chunk_id=1, input_path="/tmp/chunk.md")
        d = db.get_dispatch(did)
        assert d is not None
        assert d["chunk_id"] == 1
        assert d["state"] == "building"
        assert d["retry_count"] == 0

    def test_update_dispatch_state(self, db: ServerDB) -> None:
        did = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(did, "dispatched")
        d = db.get_dispatch(did)
        assert d["state"] == "dispatched"

    def test_update_dispatch_invalid_state(self, db: ServerDB) -> None:
        did = db.create_dispatch(chunk_id=1)
        with pytest.raises(ValueError, match="Invalid dispatch state"):
            db.update_dispatch_state(did, "invalid")

    def test_increment_retry(self, db: ServerDB) -> None:
        did = db.create_dispatch(chunk_id=1)
        assert db.increment_retry(did) == 1
        assert db.increment_retry(did) == 2
        d = db.get_dispatch(did)
        assert d["retry_count"] == 2

    def test_dispatch_with_error(self, db: ServerDB) -> None:
        did = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(did, "dispatched", error="Lint failed")
        d = db.get_dispatch(did)
        assert d["error"] == "Lint failed"

    def test_non_terminal_dispatches(self, db: ServerDB) -> None:
        d1 = db.create_dispatch(chunk_id=1)
        d2 = db.create_dispatch(chunk_id=2)
        d3 = db.create_dispatch(chunk_id=3)
        d4 = db.create_dispatch(chunk_id=4)

        db.update_dispatch_state(d1, "committed")  # terminal
        db.update_dispatch_state(d2, "dispatched")  # non-terminal
        db.update_dispatch_state(d4, "validated")  # non-terminal (needs L0 regen)
        # d3 stays 'building' — non-terminal

        non_terminal = db.get_non_terminal_dispatches()
        assert len(non_terminal) == 3
        states = {d["state"] for d in non_terminal}
        assert states == {"building", "dispatched", "validated"}

    def test_recent_dispatches(self, db: ServerDB) -> None:
        for i in range(15):
            db.create_dispatch(chunk_id=i + 1)
        recent = db.get_recent_dispatches(limit=5)
        assert len(recent) == 5
        # Most recent first
        assert recent[0]["chunk_id"] == 15

    def test_last_dispatch(self, db: ServerDB) -> None:
        db.create_dispatch(chunk_id=1)
        db.create_dispatch(chunk_id=2)
        last = db.get_last_dispatch()
        assert last is not None
        assert last["chunk_id"] == 2

    def test_no_dispatches(self, db: ServerDB) -> None:
        assert db.get_last_dispatch() is None
        assert db.get_non_terminal_dispatches() == []


class TestServerState:
    def test_update_and_get(self, db: ServerDB) -> None:
        db.update_server_state(
            last_poll_commit="abc123",
            last_poll_time="2025-01-15T12:00:00Z",
        )
        state = db.get_server_state()
        assert state["last_poll_commit"] == "abc123"
        assert state["last_poll_time"] == "2025-01-15T12:00:00Z"

    def test_invalid_key_raises(self, db: ServerDB) -> None:
        with pytest.raises(ValueError, match="Invalid server_state keys"):
            db.update_server_state(bogus="value")

    def test_update_buffer_chars(self, db: ServerDB) -> None:
        db.update_server_state(buffer_chars_total=42)
        state = db.get_server_state()
        assert state["buffer_chars_total"] == 42

    def test_session_mtime(self, db: ServerDB) -> None:
        db.update_server_state(last_session_mtime=1234567890.5)
        state = db.get_server_state()
        assert state["last_session_mtime"] == 1234567890.5

    def test_session_offset_and_tree_mtime(self, db: ServerDB) -> None:
        db.update_server_state(
            last_session_offset=2048,
            last_session_tree_mtime=1234567891.25,
        )
        state = db.get_server_state()
        assert state["last_session_offset"] == 2048
        assert state["last_session_tree_mtime"] == 1234567891.25


class TestCrashRecovery:
    def test_building_dispatches_discarded(self, db: ServerDB) -> None:
        d1 = db.create_dispatch(chunk_id=1)  # building
        d2 = db.create_dispatch(chunk_id=2)
        db.update_dispatch_state(d2, "dispatched")

        stale = db.recover_on_startup()
        # building records deleted, only dispatched returned
        assert len(stale) == 1
        assert stale[0]["state"] == "dispatched"

        # building record should be gone
        assert db.get_dispatch(d1) is None

    def test_no_stale_dispatches(self, db: ServerDB) -> None:
        d1 = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(d1, "committed")
        assert db.recover_on_startup() == []

    def test_validated_returned_for_l0_regen(self, db: ServerDB) -> None:
        d1 = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(d1, "validated")
        # validated is NOT terminal — needs L0 regen
        stale = db.recover_on_startup()
        assert len(stale) == 1
        assert stale[0]["state"] == "validated"

    def test_committed_is_terminal(self, db: ServerDB) -> None:
        d1 = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(d1, "committed")
        assert db.recover_on_startup() == []

    def test_validated_non_terminal_in_dispatches(self, db: ServerDB) -> None:
        d1 = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(d1, "validated")
        non_terminal = db.get_non_terminal_dispatches()
        assert len(non_terminal) == 1
        assert non_terminal[0]["state"] == "validated"


# ==================================================================
# Watcher Tests
# ==================================================================


class TestFileWatcher:
    def test_handler_filters_extensions(self, project: Path) -> None:
        from engram.server.watcher import _DocEventHandler

        received: list[tuple] = []

        def cb(path, typ, chars, date, meta):
            received.append((path, typ))

        handler = _DocEventHandler(cb, project)

        # Simulate events
        from watchdog.events import FileCreatedEvent
        (project / "docs" / "working" / "spec.md").write_text("content")
        handler.on_created(
            FileCreatedEvent(str(project / "docs" / "working" / "spec.md"))
        )
        assert len(received) == 1
        assert received[0][0] == "docs/working/spec.md"

    def test_handler_skips_hidden_files(self, project: Path) -> None:
        from engram.server.watcher import _DocEventHandler

        received: list[tuple] = []

        def cb(path, typ, chars, date, meta):
            received.append((path, typ))

        handler = _DocEventHandler(cb, project)

        from watchdog.events import FileCreatedEvent
        engram_file = project / ".engram" / "config.yaml"
        handler.on_created(FileCreatedEvent(str(engram_file)))
        assert len(received) == 0

    def test_handler_skips_non_doc_extensions(self, project: Path) -> None:
        from engram.server.watcher import _DocEventHandler

        received: list[tuple] = []

        def cb(path, typ, chars, date, meta):
            received.append((path, typ))

        handler = _DocEventHandler(cb, project)

        from watchdog.events import FileCreatedEvent
        py_file = project / "docs" / "working" / "script.py"
        py_file.parent.mkdir(parents=True, exist_ok=True)
        py_file.write_text("print('hi')")
        handler.on_created(FileCreatedEvent(str(py_file)))
        assert len(received) == 0

    def test_handler_detects_json_as_issue(self, project: Path) -> None:
        from engram.server.watcher import _DocEventHandler

        received: list[tuple] = []

        def cb(path, typ, chars, date, meta):
            received.append((path, typ))

        handler = _DocEventHandler(cb, project)

        from watchdog.events import FileCreatedEvent
        issues_dir = project / "local_data" / "issues"
        issues_dir.mkdir(parents=True)
        issue_file = issues_dir / "42.json"
        issue_file.write_text("{}")
        handler.on_created(FileCreatedEvent(str(issue_file)))
        assert len(received) == 1
        assert received[0][1] == "issue"


class TestSessionPoller:
    def test_poll_detects_new_sessions(self, tmp_path: Path) -> None:
        from engram.server.watcher import SessionPoller

        history = tmp_path / "history.jsonl"
        entry = {
            "sessionId": "sess1",
            "project": "/path/to/my-project",
            "display": "This is a long enough prompt for testing purposes",
            "timestamp": int(time.time() * 1000),
        }
        history.write_text(json.dumps(entry) + "\n")

        config = {
            "sources": {
                "sessions": {
                    "format": "claude-code",
                    "path": str(history),
                    "project_match": ["my-project"],
                }
            }
        }

        received: list[tuple] = []

        def cb(path, typ, chars, date, meta):
            received.append((path, typ, chars))

        poller = SessionPoller(config, cb)
        count = poller.poll()
        assert count == 1
        assert len(received) == 1
        assert received[0][1] == "prompts"

    def test_poll_no_change(self, tmp_path: Path) -> None:
        from engram.server.watcher import SessionPoller

        history = tmp_path / "history.jsonl"
        history.write_text("")

        config = {
            "sources": {
                "sessions": {
                    "format": "claude-code",
                    "path": str(history),
                    "project_match": [],
                }
            }
        }

        def cb(*args):
            pass

        poller = SessionPoller(config, cb)
        poller.poll()  # first poll establishes mtime
        assert poller.poll() == 0  # no change

    def test_poll_missing_file(self, tmp_path: Path) -> None:
        from engram.server.watcher import SessionPoller

        config = {
            "sources": {
                "sessions": {
                    "format": "claude-code",
                    "path": str(tmp_path / "nonexistent.jsonl"),
                    "project_match": [],
                }
            }
        }

        def cb(*args):
            pass

        poller = SessionPoller(config, cb)
        assert poller.poll() == 0

    def test_poll_only_emits_appended_entries(self, tmp_path: Path) -> None:
        from engram.server.watcher import SessionPoller

        history = tmp_path / "history.jsonl"
        first = {
            "sessionId": "sess1",
            "project": "/path/to/my-project",
            "display": "This is a long enough prompt for initial poll event",
            "timestamp": int(time.time() * 1000),
        }
        history.write_text(json.dumps(first) + "\n")

        config = {
            "sources": {
                "sessions": {
                    "format": "claude-code",
                    "path": str(history),
                    "project_match": ["my-project"],
                }
            }
        }

        received: list[str] = []

        def cb(path, typ, chars, date, meta):
            received.append(path)

        poller = SessionPoller(config, cb)
        assert poller.poll() == 1
        assert poller.poll() == 0

        second = {
            "sessionId": "sess2",
            "project": "/path/to/my-project",
            "display": "This appended prompt should be emitted exactly once",
            "timestamp": int(time.time() * 1000),
        }
        with open(history, "a") as fh:
            fh.write(json.dumps(second) + "\n")

        assert poller.poll() == 1
        assert received.count(".engram/sessions/sess2.md") == 1

    def test_poll_writes_session_markdown_when_project_root_set(self, tmp_path: Path) -> None:
        from engram.server.watcher import SessionPoller

        project_root = tmp_path / "project"
        project_root.mkdir()
        history = tmp_path / "history.jsonl"
        history.write_text(json.dumps({
            "sessionId": "sess1",
            "project": "/path/to/my-project",
            "display": "This is a long enough prompt for markdown session output",
            "timestamp": int(time.time() * 1000),
        }) + "\n")

        config = {
            "sources": {
                "sessions": {
                    "format": "claude-code",
                    "path": str(history),
                    "project_match": ["my-project"],
                }
            }
        }

        received: list[tuple] = []

        def cb(path, typ, chars, date, meta):
            received.append((path, typ, chars, meta))

        poller = SessionPoller(config, cb, project_root=project_root)
        assert poller.poll() == 1

        session_file = project_root / ".engram" / "sessions" / "sess1.md"
        assert session_file.exists()
        assert "long enough prompt" in session_file.read_text()
        assert received[0][0] == ".engram/sessions/sess1.md"
        assert received[0][2] == session_file.stat().st_size

    def test_codex_tree_change_can_unlock_project_match(self, tmp_path: Path) -> None:
        from engram.server.watcher import SessionPoller

        codex_home = tmp_path / ".codex"
        codex_home.mkdir(parents=True)
        history = codex_home / "history.jsonl"
        history.write_text(json.dumps({
            "session_id": "11111111-1111-1111-1111-111111111111",
            "ts": 1771641000,
            "text": "Codex prompt long enough to pass filtering constraints",
        }) + "\n")

        config = {
            "sources": {
                "sessions": {
                    "format": "codex",
                    "path": str(history),
                    "project_match": ["my-project"],
                }
            }
        }

        received: list[str] = []

        def cb(path, typ, chars, date, meta):
            received.append(path)

        poller = SessionPoller(config, cb)
        assert poller.poll() == 0  # No codex session cwd metadata yet.

        sessions_dir = codex_home / "sessions" / "2026" / "02" / "21"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "rollout-2026-02-21-11111111-1111-1111-1111-111111111111.jsonl").write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "cwd": "/Users/dev/my-project",
                },
            }) + "\n",
        )

        assert poller.poll() == 1
        assert received == [".engram/sessions/11111111-1111-1111-1111-111111111111.md"]


class TestGitPoller:
    def test_first_poll_records_head(self, tmp_path: Path) -> None:
        from engram.server.watcher import GitPoller

        received: list[tuple] = []

        def cb(path, typ, chars, date, meta):
            received.append(path)

        poller = GitPoller(tmp_path, cb)

        # Mock subprocess to return a commit hash
        with patch("engram.server.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="abc123\n"
            )
            commits = poller.poll()
            assert commits == []  # first poll just records HEAD
            assert poller.get_last_commit() == "abc123"

    def test_no_new_commits(self, tmp_path: Path) -> None:
        from engram.server.watcher import GitPoller

        def cb(*args):
            pass

        poller = GitPoller(tmp_path, cb)
        poller.set_last_commit("abc123")

        with patch("engram.server.watcher.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="abc123\n"
            )
            commits = poller.poll()
            assert commits == []

    def test_new_commits_uses_old_bookmark_for_diff(self, tmp_path: Path) -> None:
        """Verify git diff uses old..new range, not the overwritten bookmark."""
        from engram.server.watcher import GitPoller

        received: list[str] = []

        def cb(path, typ, chars, date, meta):
            received.append(path)

        poller = GitPoller(tmp_path, cb)
        poller.set_last_commit("old_abc")

        call_count = 0

        def mock_run_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd = args[0]
            if cmd[1] == "rev-parse":
                return MagicMock(returncode=0, stdout="new_def\n")
            elif cmd[1] == "log":
                return MagicMock(returncode=0, stdout="new_def\n")
            elif cmd[1] == "diff":
                # Verify the diff range uses old..new
                diff_range = cmd[3]
                assert diff_range == "old_abc..new_def", (
                    f"Expected 'old_abc..new_def', got '{diff_range}'"
                )
                return MagicMock(returncode=0, stdout="changed_file.md\n")
            return MagicMock(returncode=0, stdout="")

        with patch("engram.server.watcher.subprocess.run", side_effect=mock_run_side_effect):
            commits = poller.poll()
            assert commits == ["new_def"]
            assert poller.get_last_commit() == "new_def"


# ==================================================================
# ContextBuffer Tests
# ==================================================================


class TestContextBuffer:
    def test_add_item(self, project: Path, config: dict) -> None:
        from engram.server.buffer import ContextBuffer

        db = ServerDB(project / ".engram" / "engram.db")
        buffer = ContextBuffer(config, project, db)

        assert buffer.add_item("docs/spec.md", "doc", 100) is True
        items = buffer.get_items()
        assert len(items) == 1

    def test_duplicate_rejected(self, project: Path, config: dict) -> None:
        from engram.server.buffer import ContextBuffer

        db = ServerDB(project / ".engram" / "engram.db")
        buffer = ContextBuffer(config, project, db)

        assert buffer.add_item("docs/spec.md", "doc", 100) is True
        assert buffer.add_item("docs/spec.md", "doc", 100) is False
        assert len(buffer.get_items()) == 1

    def test_should_dispatch_empty_buffer(self, project: Path, config: dict) -> None:
        from engram.server.buffer import ContextBuffer

        db = ServerDB(project / ".engram" / "engram.db")
        buffer = ContextBuffer(config, project, db)
        assert buffer.should_dispatch() is None

    def test_should_dispatch_buffer_full(self, project: Path, config: dict) -> None:
        from engram.server.buffer import ContextBuffer

        # Set max_chunk_chars very low to trigger
        config["budget"]["max_chunk_chars"] = 100
        db = ServerDB(project / ".engram" / "engram.db")
        buffer = ContextBuffer(config, project, db)

        buffer.add_item("docs/spec.md", "doc", 200)
        reason = buffer.should_dispatch()
        assert reason == "buffer_full"

    def test_fill_info(self, project: Path, config: dict) -> None:
        from engram.server.buffer import ContextBuffer

        db = ServerDB(project / ".engram" / "engram.db")
        buffer = ContextBuffer(config, project, db)
        buffer.add_item("docs/spec.md", "doc", 1000)

        info = buffer.get_fill_info()
        assert info["item_count"] == 1
        assert info["buffer_chars"] == 1000
        assert info["budget"] > 0
        assert info["fill_pct"] >= 0

    def test_consume_all(self, project: Path, config: dict) -> None:
        from engram.server.buffer import ContextBuffer

        db = ServerDB(project / ".engram" / "engram.db")
        buffer = ContextBuffer(config, project, db)

        buffer.add_item("a.md", "doc", 100)
        buffer.add_item("b.md", "doc", 200)
        consumed = buffer.consume_all()
        assert len(consumed) == 2
        assert len(buffer.get_items()) == 0


# ==================================================================
# Dispatcher Tests
# ==================================================================


class TestDispatcherHelpers:
    def test_read_docs(self, project: Path, config: dict) -> None:
        from engram.config import resolve_doc_paths
        from engram.dispatch import read_docs

        doc_paths = resolve_doc_paths(config, project)
        contents = read_docs(doc_paths, ("timeline", "concepts"))
        assert "# Timeline" in contents["timeline"]
        assert "# Concept Registry" in contents["concepts"]

    def test_inject_section_new(self, tmp_path: Path) -> None:
        from engram.server.briefing import _inject_section

        f = tmp_path / "test.md"
        f.write_text("# Existing\nContent here.\n")

        _inject_section(f, "## New Section", "New content here.")
        text = f.read_text()
        assert "## New Section" in text
        assert "New content here." in text
        assert "# Existing" in text

    def test_inject_section_replace(self, tmp_path: Path) -> None:
        from engram.server.briefing import _inject_section

        f = tmp_path / "test.md"
        f.write_text(
            "# Main\n\n## Knowledge\n\nOld briefing.\n\n## Other\n\nOther content.\n"
        )

        _inject_section(f, "## Knowledge", "Updated briefing.")
        text = f.read_text()
        assert "Updated briefing." in text
        assert "Old briefing." not in text
        assert "## Other" in text
        assert "Other content." in text

    def test_build_correction_text(self) -> None:
        from engram.server.dispatcher import _build_correction_text

        # Minimal ChunkResult-like object
        chunk = MagicMock()
        chunk.chunk_id = 42
        chunk.input_path.resolve.return_value = "/tmp/chunk.md"

        from engram.linter.schema import Violation
        from engram.linter import LintResult
        result = LintResult(
            passed=False,
            violations=[
                Violation(doc_type="concepts", entry_id="C001", message="Missing Code: field"),
            ],
        )
        text = _build_correction_text(chunk, result)
        assert "CORRECTION REQUIRED" in text
        assert "chunk 42" in text
        assert "Missing Code: field" in text
        assert "[concepts/C001]" in text

    def test_build_correction_text_from_lint(self) -> None:
        from engram.server.dispatcher import _build_correction_text_from_lint

        from engram.linter.schema import Violation
        from engram.linter import LintResult
        result = LintResult(
            passed=False,
            violations=[
                Violation(doc_type="epistemic", entry_id="E005", message="Missing Evidence:"),
            ],
        )
        text = _build_correction_text_from_lint(result)
        assert "CORRECTION REQUIRED" in text
        assert "Missing Evidence:" in text

    def test_flush_buffer_to_queue_consumes_and_writes_entries(
        self,
        project: Path,
        config: dict,
    ) -> None:
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        db.add_buffer_item("docs/working/live.md", "doc", 120, "2026-02-21T10:00:00Z")

        issue_dir = project / "local_data" / "issues"
        issue_dir.mkdir(parents=True, exist_ok=True)
        (issue_dir / "77.json").write_text(json.dumps({
            "number": 77,
            "title": "Live issue title",
            "body": "Body",
        }))
        db.add_buffer_item("local_data/issues/77.json", "issue", 40, "2026-02-21T10:01:00Z")

        session_dir = project / ".engram" / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "sess-1.md").write_text("prompt text")
        db.add_buffer_item(
            ".engram/sessions/sess-1.md",
            "prompts",
            11,
            "2026-02-21T10:02:00Z",
            metadata=json.dumps({"prompt_count": 3}),
        )

        added = dispatcher._flush_buffer_to_queue()
        assert added == 3
        assert db.get_buffer_items() == []

        queue_file = project / ".engram" / "queue.jsonl"
        rows = [json.loads(line) for line in queue_file.read_text().splitlines() if line.strip()]
        by_type = {row["type"]: row for row in rows}
        assert by_type["doc"]["path"] == "docs/working/live.md"
        assert by_type["doc"]["pass"] == "revisit"
        assert by_type["issue"]["issue_number"] == 77
        assert by_type["issue"]["issue_title"] == "Live issue title"
        assert by_type["prompts"]["session_id"] == "sess-1"
        assert by_type["prompts"]["prompt_count"] == 3

    def test_dispatch_calls_buffer_flush_before_chunk_build(
        self,
        project: Path,
        config: dict,
    ) -> None:
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        with patch.object(dispatcher, "_flush_buffer_to_queue", return_value=0) as mock_flush:
            with patch("engram.server.dispatcher.next_chunk", side_effect=ValueError("Queue is empty")):
                assert dispatcher.dispatch() is False

        mock_flush.assert_called_once()


class TestDispatcherRecovery:
    def test_recover_validated_marks_stale(self, project: Path, config: dict) -> None:
        """Validated dispatch should mark L0 stale and transition to committed."""
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        # Create a validated dispatch
        did = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(did, "dispatched")
        db.update_dispatch_state(did, "validated")

        dispatch = db.get_dispatch(did)
        result = dispatcher.recover_dispatch(dispatch)

        assert result is True
        assert db.is_l0_stale() is True
        final = db.get_dispatch(did)
        assert final["state"] == "committed"

    def test_recover_dispatched_lint_passes(self, project: Path, config: dict) -> None:
        """Dispatched dispatch with passing lint should mark stale and commit."""
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        # Create a dispatch with input file
        chunks_dir = project / ".engram" / "chunks"
        chunks_dir.mkdir(parents=True)
        input_path = chunks_dir / "chunk_001_input.md"
        input_path.write_text("test input")

        did = db.create_dispatch(chunk_id=1, input_path=str(input_path))
        db.update_dispatch_state(did, "dispatched")

        dispatch = db.get_dispatch(did)
        result = dispatcher.recover_dispatch(dispatch)

        assert result is True
        assert db.is_l0_stale() is True
        final = db.get_dispatch(did)
        assert final["state"] == "committed"


# ==================================================================
# Server Status Tests
# ==================================================================


class TestGetStatus:
    def test_no_db(self, project: Path, config: dict) -> None:
        from engram.server import get_status

        # Remove the db file if it exists
        db_file = project / ".engram" / "engram.db"
        if db_file.exists():
            db_file.unlink()

        info = get_status(config, project)
        assert "error" in info

    def test_with_db(self, project: Path, config: dict) -> None:
        from engram.server import get_status

        # Create db by instantiating ServerDB
        ServerDB(project / ".engram" / "engram.db")

        info = get_status(config, project)
        assert "buffer" in info
        assert info["buffer"]["item_count"] == 0
        assert info["pending_items"] == 0
        assert info["last_dispatch"] is None


# ==================================================================
# CLI Tests
# ==================================================================


class TestCLIStatus:
    def test_status_no_db(self, project: Path) -> None:
        from click.testing import CliRunner
        from engram.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--project-root", str(project)])
        # Should show error about no database
        assert result.exit_code == 1 or "Error" in result.output or "No database" in result.output

    def test_status_with_db(self, project: Path) -> None:
        from click.testing import CliRunner
        from engram.cli import cli

        # Create DB
        ServerDB(project / ".engram" / "engram.db")

        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--project-root", str(project)])
        assert result.exit_code == 0
        assert "Buffer:" in result.output
        assert "Pending items:" in result.output


class TestCLIRun:
    def test_run_registered(self) -> None:
        """Verify 'run' command is registered in CLI group."""
        from engram.cli import cli
        assert "run" in [c.name for c in cli.commands.values()]

    def test_status_registered(self) -> None:
        """Verify 'status' command is registered in CLI group."""
        from engram.cli import cli
        assert "status" in [c.name for c in cli.commands.values()]


# ==================================================================
# Context Manager Tests
# ==================================================================


class TestContextManager:
    def test_db_context_manager(self, db_path: Path) -> None:
        with ServerDB(db_path) as db:
            db.add_buffer_item("test.md", "doc", 100)
            items = db.get_buffer_items()
            assert len(items) == 1
