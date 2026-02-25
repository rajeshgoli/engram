"""Tests for engram.fold.queue."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from engram.config import DEFAULTS, _deep_merge
from engram.fold.queue import REVISIT_THRESHOLD_DAYS, build_queue, refresh_issue_snapshots


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a minimal project with .engram config, docs, and issues."""
    root = tmp_path / "project"
    root.mkdir()

    # Create .engram/config.yaml
    engram_dir = root / ".engram"
    engram_dir.mkdir()

    config = {
        "sources": {
            "issues": "issues/",
            "docs": ["docs/working/"],
            "sessions": {
                "format": "claude-code",
                "path": str(tmp_path / "history.jsonl"),
                "project_match": ["project"],
            },
        },
    }
    (engram_dir / "config.yaml").write_text(yaml.dump(config))

    # Create doc dirs
    working = root / "docs" / "working"
    working.mkdir(parents=True)

    # Create issues dir
    issues = root / "issues"
    issues.mkdir()

    return root


def _make_config(project: Path, overrides: dict | None = None) -> dict:
    """Build a full config dict with optional overrides."""
    base = {
        "sources": {
            "issues": "issues/",
            "docs": ["docs/working/"],
            "sessions": {
                "format": "claude-code",
                "path": str(project.parent / "history.jsonl"),
                "project_match": [],
            },
        },
    }
    if overrides:
        base = _deep_merge(base, overrides)
    return _deep_merge(DEFAULTS, base)


def _mock_git_run(cmd, **kwargs):
    """Mock subprocess.run for git commands — returns empty/no-op."""
    return type("Result", (), {"stdout": "\n", "returncode": 0})()


class TestBuildQueueDocs:
    def test_includes_doc_entries(self, project: Path) -> None:
        # Create a doc
        doc = project / "docs" / "working" / "1001_test_spec.md"
        doc.write_text("**Date:** 2026-01-15\n\n# Test Spec\n\nContent here.")

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project)

        doc_entries = [e for e in entries if e["type"] == "doc"]
        assert len(doc_entries) >= 1
        assert doc_entries[0]["path"] == "docs/working/1001_test_spec.md"
        assert doc_entries[0]["pass"] == "initial"

    def test_doc_frontmatter_date_used(self, project: Path) -> None:
        doc = project / "docs" / "working" / "spec.md"
        doc.write_text("**Date:** 2026-01-20\n\nContent.")

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project)

        doc_entries = [e for e in entries if e["type"] == "doc"]
        assert doc_entries[0]["date"].startswith("2026-01-20")

    def test_doc_revisit_entry(self, project: Path) -> None:
        """Docs modified much later than created get a revisit entry."""
        doc = project / "docs" / "working" / "evolving.md"
        doc.write_text("**Date:** 2026-01-01\n\nContent.")

        # Mock git: created Jan 1, modified Feb 15 (>7 days apart)
        def mock_run(cmd, **kwargs):
            if "--diff-filter=A" in cmd:
                return type("R", (), {
                    "stdout": "2026-01-01T00:00:00-06:00\nfile.md\n",
                    "returncode": 0,
                })()
            elif "-1" in cmd:
                return type("R", (), {
                    "stdout": "2026-02-15T00:00:00-06:00\n",
                    "returncode": 0,
                })()
            return type("R", (), {"stdout": "\n", "returncode": 0})()

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=mock_run):
            entries = build_queue(config, project)

        doc_entries = [e for e in entries if e["type"] == "doc"]
        passes = [e["pass"] for e in doc_entries]
        assert "initial" in passes
        assert "revisit" in passes


class TestBuildQueueIssues:
    def test_includes_issue_entries(self, project: Path) -> None:
        issue = {
            "number": 42,
            "title": "Bug report",
            "body": "Something is wrong.",
            "createdAt": "2026-01-10T12:00:00Z",
            "state": "OPEN",
            "labels": [],
            "comments": [],
        }
        (project / "issues" / "42.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project)

        issue_entries = [e for e in entries if e["type"] == "issue"]
        assert len(issue_entries) == 1
        assert issue_entries[0]["issue_number"] == 42
        assert issue_entries[0]["issue_title"] == "Bug report"
        assert issue_entries[0]["date"] == "2026-01-10T12:00:00Z"


class TestBuildQueueSessions:
    def test_includes_session_entries(self, project: Path) -> None:
        now_ms = int(time.time() * 1000)
        history = project.parent / "history.jsonl"
        with open(history, "w") as f:
            f.write(json.dumps({
                "sessionId": "s1",
                "project": "/path/to/project",
                "display": "Implement the authentication feature now",
                "timestamp": now_ms,
            }) + "\n")

        config = _make_config(project, {
            "sources": {
                "sessions": {
                    "path": str(history),
                    "project_match": ["project"],
                },
            },
        })

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project)

        session_entries = [e for e in entries if e["type"] == "prompts"]
        assert len(session_entries) == 1
        assert session_entries[0]["session_id"] == "s1"
        assert session_entries[0]["prompt_count"] == 1

    def test_session_files_written(self, project: Path) -> None:
        now_ms = int(time.time() * 1000)
        history = project.parent / "history.jsonl"
        with open(history, "w") as f:
            f.write(json.dumps({
                "sessionId": "s-abc",
                "project": "/dev/project",
                "display": "A long enough prompt to pass filter checks",
                "timestamp": now_ms,
            }) + "\n")

        config = _make_config(project, {
            "sources": {
                "sessions": {
                    "path": str(history),
                    "project_match": ["project"],
                },
            },
        })

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            build_queue(config, project)

        session_file = project / ".engram" / "sessions" / "s-abc.md"
        assert session_file.exists()
        content = session_file.read_text()
        assert "long enough prompt" in content

    def test_includes_codex_session_entries(self, project: Path) -> None:
        codex_home = project.parent / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        history = codex_home / "history.jsonl"
        history.write_text(
            json.dumps({
                "session_id": "11111111-1111-1111-1111-111111111111",
                "ts": 1771641000,
                "text": "Implement codex adapter behavior with rich project context",
            }) + "\n"
            + json.dumps({
                "session_id": "22222222-2222-2222-2222-222222222222",
                "ts": 1771641060,
                "text": "This belongs to a different repo and should be filtered out",
            }) + "\n",
        )

        sessions_dir = codex_home / "sessions" / "2026" / "02" / "21"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "rollout-2026-02-21-11111111-1111-1111-1111-111111111111.jsonl").write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "cwd": "/Users/dev/project",
                },
            }) + "\n",
        )
        (sessions_dir / "rollout-2026-02-21-22222222-2222-2222-2222-222222222222.jsonl").write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "cwd": "/Users/dev/other-repo",
                },
            }) + "\n",
        )

        config = _make_config(project, {
            "sources": {
                "sessions": {
                    "format": "codex",
                    "path": str(history),
                    "project_match": ["project"],
                },
            },
        })

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project)

        session_entries = [e for e in entries if e["type"] == "prompts"]
        assert len(session_entries) == 1
        assert session_entries[0]["session_id"] == "11111111-1111-1111-1111-111111111111"

        session_file = (
            project / ".engram" / "sessions" / "11111111-1111-1111-1111-111111111111.md"
        )
        assert session_file.exists()
        assert "codex adapter behavior" in session_file.read_text()


class TestBuildQueueSorting:
    def test_entries_sorted_by_date(self, project: Path) -> None:
        # Create issue dated Jan 1
        issue = {
            "number": 1, "title": "Early", "body": "First",
            "createdAt": "2026-01-01T00:00:00Z",
            "state": "OPEN", "labels": [], "comments": [],
        }
        (project / "issues" / "1.json").write_text(json.dumps(issue))

        # Create doc dated Feb 1
        doc = project / "docs" / "working" / "later.md"
        doc.write_text("**Date:** 2026-02-01\n\nLater content.")

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project)

        dates = [e["date"] for e in entries]
        assert dates == sorted(dates)


class TestBuildQueueOutput:
    def test_writes_jsonl(self, project: Path) -> None:
        issue = {
            "number": 1, "title": "Test", "body": "Body",
            "createdAt": "2026-01-15T00:00:00Z",
            "state": "OPEN", "labels": [], "comments": [],
        }
        (project / "issues" / "1.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            build_queue(config, project)

        queue_file = project / ".engram" / "queue.jsonl"
        assert queue_file.exists()

        with open(queue_file) as f:
            lines = f.readlines()
        assert len(lines) >= 1

        # Each line is valid JSON
        for line in lines:
            parsed = json.loads(line)
            assert "date" in parsed
            assert "type" in parsed

    def test_writes_sizes(self, project: Path) -> None:
        issue = {
            "number": 1, "title": "Test", "body": "Body",
            "createdAt": "2026-01-15T00:00:00Z",
            "state": "OPEN", "labels": [], "comments": [],
        }
        (project / "issues" / "1.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            build_queue(config, project)

        sizes_file = project / ".engram" / "item_sizes.json"
        assert sizes_file.exists()
        sizes = json.loads(sizes_file.read_text())
        assert isinstance(sizes, dict)
        assert len(sizes) >= 1


class TestBuildQueueEmpty:
    def test_empty_project(self, project: Path) -> None:
        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project)

        assert entries == []


class TestBuildQueueStartDate:
    def test_filters_entries_before_date(self, project: Path) -> None:
        """Entries before start_date are excluded."""
        # Create issues with different dates
        for num, dt in [(1, "2025-12-01T00:00:00Z"), (2, "2026-01-15T00:00:00Z"),
                        (3, "2026-02-01T00:00:00Z")]:
            issue = {
                "number": num, "title": f"Issue {num}", "body": "Body",
                "createdAt": dt, "state": "OPEN", "labels": [], "comments": [],
            }
            (project / "issues" / f"{num}.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project, start_date="2026-01-01")

        assert len(entries) == 2
        dates = [e["date"][:10] for e in entries]
        assert "2025-12-01" not in dates
        assert "2026-01-15" in dates
        assert "2026-02-01" in dates

    def test_none_start_date_returns_all(self, project: Path) -> None:
        """start_date=None returns full queue (no filtering)."""
        for num, dt in [(1, "2025-06-01T00:00:00Z"), (2, "2026-01-15T00:00:00Z")]:
            issue = {
                "number": num, "title": f"Issue {num}", "body": "Body",
                "createdAt": dt, "state": "OPEN", "labels": [], "comments": [],
            }
            (project / "issues" / f"{num}.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project, start_date=None)

        assert len(entries) == 2

    def test_all_filtered_out(self, project: Path) -> None:
        """If all entries are before start_date, returns empty list."""
        issue = {
            "number": 1, "title": "Old", "body": "Body",
            "createdAt": "2024-01-01T00:00:00Z",
            "state": "OPEN", "labels": [], "comments": [],
        }
        (project / "issues" / "1.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project, start_date="2026-01-01")

        assert entries == []

    def test_rejects_non_yyyy_mm_dd(self, project: Path) -> None:
        """start_date with datetime ISO string raises ValueError."""
        issue = {
            "number": 1, "title": "Test", "body": "Body",
            "createdAt": "2026-01-15T00:00:00Z",
            "state": "OPEN", "labels": [], "comments": [],
        }
        (project / "issues" / "1.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            with pytest.raises(ValueError):
                build_queue(config, project, start_date="2026-01-01T00:00:00")

    def test_queue_jsonl_only_has_filtered_entries(self, project: Path) -> None:
        """queue.jsonl output only contains post-cutoff entries."""
        for num, dt in [(1, "2025-06-01T00:00:00Z"), (2, "2026-02-01T00:00:00Z")]:
            issue = {
                "number": num, "title": f"Issue {num}", "body": "Body",
                "createdAt": dt, "state": "OPEN", "labels": [], "comments": [],
            }
            (project / "issues" / f"{num}.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            build_queue(config, project, start_date="2026-01-01")

        queue_file = project / ".engram" / "queue.jsonl"
        with open(queue_file) as f:
            lines = [json.loads(line) for line in f if line.strip()]

        assert len(lines) == 1
        assert lines[0]["date"][:10] == "2026-02-01"

    def test_item_sizes_unfiltered(self, project: Path) -> None:
        """item_sizes.json includes ALL artifacts regardless of start_date."""
        for num, dt in [(1, "2025-06-01T00:00:00Z"), (2, "2026-02-01T00:00:00Z")]:
            issue = {
                "number": num, "title": f"Issue {num}", "body": "Body",
                "createdAt": dt, "state": "OPEN", "labels": [], "comments": [],
            }
            (project / "issues" / f"{num}.json").write_text(json.dumps(issue))

        config = _make_config(project)

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            build_queue(config, project, start_date="2026-01-01")

        sizes_file = project / ".engram" / "item_sizes.json"
        sizes = json.loads(sizes_file.read_text())
        # Both issues should appear in sizes even though one is filtered
        assert len(sizes) == 2

    def test_session_files_only_for_surviving_entries(self, project: Path) -> None:
        """Session .md files are only written for entries that survive the filter."""
        # Create two sessions: one old, one recent
        history = project.parent / "history.jsonl"
        old_ts = 1704067200000   # 2024-01-01T00:00:00Z (ms)
        new_ts = 1738368000000   # 2025-02-01T00:00:00Z (ms)
        with open(history, "w") as f:
            f.write(json.dumps({
                "sessionId": "old-session",
                "project": "/path/to/project",
                "display": "Old session content that should be filtered out",
                "timestamp": old_ts,
            }) + "\n")
            f.write(json.dumps({
                "sessionId": "new-session",
                "project": "/path/to/project",
                "display": "New session content that should survive the filter",
                "timestamp": new_ts,
            }) + "\n")

        config = _make_config(project, {
            "sources": {
                "sessions": {
                    "path": str(history),
                    "project_match": ["project"],
                },
            },
        })

        with patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run):
            entries = build_queue(config, project, start_date="2025-01-01")

        sessions_dir = project / ".engram" / "sessions"
        session_entries = [e for e in entries if e["type"] == "prompts"]

        # The new session should survive; old should be filtered
        session_ids = [e["session_id"] for e in session_entries]
        assert "new-session" in session_ids
        assert "old-session" not in session_ids

        # Only surviving session file should exist
        assert (sessions_dir / "new-session.md").exists()
        assert not (sessions_dir / "old-session.md").exists()


class TestIssueRefresh:
    def test_refresh_uses_explicit_repo(self, project: Path) -> None:
        config = _make_config(project, {"sources": {"github_repo": "owner/repo"}})

        with patch("engram.fold.queue.pull_issues", return_value=[{"number": 1}]) as mock_pull:
            ok, message = refresh_issue_snapshots(config, project)

        assert ok is True
        assert "refreshed 1 issues from owner/repo" in message
        mock_pull.assert_called_once_with("owner/repo", project / "issues")

    def test_refresh_skips_when_repo_unresolved(self, project: Path) -> None:
        config = _make_config(project, {"sources": {"github_repo": None}})

        with patch("engram.fold.queue.infer_github_repo", return_value=None):
            ok, message = refresh_issue_snapshots(config, project)

        assert ok is True
        assert "using local issue snapshots" in message

    def test_refresh_returns_failure_when_gh_fails(self, project: Path) -> None:
        config = _make_config(project, {"sources": {"github_repo": "owner/repo"}})

        called_process_error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "issue", "list"],
            stderr="authentication failed",
        )

        with patch("engram.fold.queue.pull_issues", side_effect=called_process_error):
            ok, message = refresh_issue_snapshots(config, project)

        assert ok is False
        assert "gh issue list failed for owner/repo" in message


class TestGitTrackedDocDiscovery:
    def test_prefers_git_tracked_docs_when_available(self, project: Path) -> None:
        tracked = project / "docs" / "working" / "tracked.md"
        untracked = project / "docs" / "working" / "scratch.md"
        tracked.write_text("**Date:** 2026-01-01\n\nTracked")
        untracked.write_text("**Date:** 2026-01-01\n\nUntracked")

        config = _make_config(project)

        with (
            patch("engram.fold.queue.list_tracked_markdown_docs", return_value=[tracked]),
            patch("engram.fold.sources.subprocess.run", side_effect=_mock_git_run),
        ):
            entries = build_queue(config, project)

        doc_paths = [e["path"] for e in entries if e["type"] == "doc"]
        assert doc_paths == ["docs/working/tracked.md"]
