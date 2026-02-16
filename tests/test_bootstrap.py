"""Tests for engram bootstrap: seed + forward fold."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from engram.bootstrap.fold import _filter_queue_by_date, forward_fold
from engram.bootstrap.seed import (
    _collect_repo_snapshot,
    _ensure_living_docs,
    _find_commit_at_date,
    seed,
)
from engram.cli import GRAVEYARD_HEADERS, LIVING_DOC_HEADERS, cli
from engram.config import load_config


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Set up a minimal engram project with git repo."""
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
sources:
  issues: local_data/issues/
  docs:
    - docs/working/
  sessions:
    format: claude-code
    path: /dev/null
    project_match: []
budget:
  context_limit_chars: 600000
  instructions_overhead: 10000
  max_chunk_chars: 200000
model: sonnet
"""
    (engram_dir / "config.yaml").write_text(config_yaml)

    docs_dir = tmp_path / "docs" / "decisions"
    docs_dir.mkdir(parents=True)
    (docs_dir / "timeline.md").write_text(LIVING_DOC_HEADERS["timeline"])
    (docs_dir / "concept_registry.md").write_text(LIVING_DOC_HEADERS["concepts"])
    (docs_dir / "epistemic_state.md").write_text(LIVING_DOC_HEADERS["epistemic"])
    (docs_dir / "workflow_registry.md").write_text(LIVING_DOC_HEADERS["workflows"])
    (docs_dir / "concept_graveyard.md").write_text(GRAVEYARD_HEADERS["concepts"])
    (docs_dir / "epistemic_graveyard.md").write_text(GRAVEYARD_HEADERS["epistemic"])

    # Init git repo
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(tmp_path), capture_output=True,
    )

    return tmp_path


# ------------------------------------------------------------------
# seed.py tests
# ------------------------------------------------------------------


class TestFindCommitAtDate:
    def test_finds_commit(self, project: Path) -> None:
        sha = _find_commit_at_date(project, date.today())
        assert len(sha) == 40

    def test_no_commit_before_epoch(self, project: Path) -> None:
        with pytest.raises(ValueError, match="No commit found"):
            _find_commit_at_date(project, date(1970, 1, 1))


class TestEnsureLivingDocs:
    def test_creates_missing_docs(self, tmp_path: Path) -> None:
        engram_dir = tmp_path / ".engram"
        engram_dir.mkdir()
        config_yaml = """\
living_docs:
  timeline: docs/timeline.md
  concepts: docs/concepts.md
  epistemic: docs/epistemic.md
  workflows: docs/workflows.md
graveyard:
  concepts: docs/concept_graveyard.md
  epistemic: docs/epistemic_graveyard.md
"""
        (engram_dir / "config.yaml").write_text(config_yaml)
        config = load_config(tmp_path)

        _ensure_living_docs(tmp_path, config)

        for key in ("timeline", "concepts", "epistemic", "workflows"):
            path = tmp_path / config["living_docs"][key]
            assert path.exists()
            assert path.read_text() == LIVING_DOC_HEADERS[key]

        for key in ("concepts", "epistemic"):
            path = tmp_path / config["graveyard"][key]
            assert path.exists()
            assert path.read_text() == GRAVEYARD_HEADERS[key]

    def test_preserves_existing_docs(self, project: Path) -> None:
        config = load_config(project)
        timeline_path = project / config["living_docs"]["timeline"]
        timeline_path.write_text("# Timeline\n\nExisting content\n")

        _ensure_living_docs(project, config)

        assert "Existing content" in timeline_path.read_text()


class TestCollectRepoSnapshot:
    def test_includes_directory_structure(self, project: Path) -> None:
        config = load_config(project)
        snapshot = _collect_repo_snapshot(project, config)
        assert "Repository Structure" in snapshot

    def test_includes_readme(self, project: Path) -> None:
        (project / "README.md").write_text("# Test Project\n\nA test.\n")
        config = load_config(project)
        snapshot = _collect_repo_snapshot(project, config)
        assert "Test Project" in snapshot

    def test_includes_docs(self, project: Path) -> None:
        working_dir = project / "docs" / "working"
        working_dir.mkdir(parents=True)
        (working_dir / "spec.md").write_text("# Spec\n\nDesign details.\n")
        config = load_config(project)
        snapshot = _collect_repo_snapshot(project, config)
        assert "Design details" in snapshot

    def test_truncates_large_files(self, project: Path) -> None:
        (project / "README.md").write_text("x" * 20_000)
        config = load_config(project)
        snapshot = _collect_repo_snapshot(project, config)
        # Should be truncated to 10K
        assert len(snapshot) < 20_000


class TestSeed:
    @patch("engram.bootstrap.seed._dispatch_seed_agent")
    def test_path_b_no_worktree(self, mock_dispatch: MagicMock, project: Path) -> None:
        """Path B: seed from today should not create a worktree."""
        mock_dispatch.return_value = True
        result = seed(project, from_date=None)
        assert result is True
        mock_dispatch.assert_called_once()
        # Verify snapshot content was from project root (not a worktree)
        call_args = mock_dispatch.call_args
        assert call_args[0][0] == project  # project_root arg

    @patch("engram.bootstrap.seed._dispatch_seed_agent")
    def test_dispatch_failure_returns_false(self, mock_dispatch: MagicMock, project: Path) -> None:
        mock_dispatch.return_value = False
        result = seed(project, from_date=None)
        assert result is False

    @patch("engram.bootstrap.seed._dispatch_seed_agent")
    @patch("engram.bootstrap.seed._remove_worktree")
    @patch("engram.bootstrap.seed._create_worktree")
    @patch("engram.bootstrap.seed._find_commit_at_date")
    def test_path_a_creates_and_cleans_worktree(
        self,
        mock_find: MagicMock,
        mock_create: MagicMock,
        mock_remove: MagicMock,
        mock_dispatch: MagicMock,
        project: Path,
        tmp_path: Path,
    ) -> None:
        """Path A: worktree created and cleaned up."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        mock_find.return_value = "abc123" * 7  # 42 char sha, truncated doesn't matter
        mock_create.return_value = worktree
        mock_dispatch.return_value = True

        # Mock forward_fold to avoid actually running
        with patch("engram.bootstrap.fold.forward_fold", return_value=True):
            result = seed(project, from_date=date(2026, 1, 1))

        assert result is True
        mock_create.assert_called_once()
        mock_remove.assert_called_once_with(project, worktree)

    @patch("engram.bootstrap.seed._dispatch_seed_agent")
    @patch("engram.bootstrap.seed._remove_worktree")
    @patch("engram.bootstrap.seed._create_worktree")
    @patch("engram.bootstrap.seed._find_commit_at_date")
    def test_worktree_cleanup_on_error(
        self,
        mock_find: MagicMock,
        mock_create: MagicMock,
        mock_remove: MagicMock,
        mock_dispatch: MagicMock,
        project: Path,
        tmp_path: Path,
    ) -> None:
        """Worktree is cleaned up even if dispatch raises."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        mock_find.return_value = "abc123" * 7
        mock_create.return_value = worktree
        mock_dispatch.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            seed(project, from_date=date(2026, 1, 1))

        mock_remove.assert_called_once_with(project, worktree)


# ------------------------------------------------------------------
# fold.py tests
# ------------------------------------------------------------------


class TestFilterQueueByDate:
    def test_filters_entries(self, project: Path) -> None:
        queue_file = project / ".engram" / "queue.jsonl"
        entries = [
            {"date": "2025-12-01T00:00:00Z", "type": "doc", "path": "a.md", "chars": 100},
            {"date": "2026-01-15T00:00:00Z", "type": "doc", "path": "b.md", "chars": 200},
            {"date": "2026-02-01T00:00:00Z", "type": "doc", "path": "c.md", "chars": 300},
        ]
        with open(queue_file, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        remaining = _filter_queue_by_date(project, date(2026, 1, 1))
        assert remaining == 2

        with open(queue_file) as fh:
            filtered = [json.loads(line) for line in fh if line.strip()]
        paths = [e["path"] for e in filtered]
        assert "a.md" not in paths
        assert "b.md" in paths
        assert "c.md" in paths

    def test_empty_queue(self, project: Path) -> None:
        queue_file = project / ".engram" / "queue.jsonl"
        queue_file.write_text("")
        remaining = _filter_queue_by_date(project, date(2026, 1, 1))
        assert remaining == 0

    def test_all_filtered_out(self, project: Path) -> None:
        queue_file = project / ".engram" / "queue.jsonl"
        entries = [
            {"date": "2024-01-01T00:00:00Z", "type": "doc", "path": "old.md", "chars": 100},
        ]
        with open(queue_file, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        remaining = _filter_queue_by_date(project, date(2026, 1, 1))
        assert remaining == 0

    def test_no_queue_file(self, project: Path) -> None:
        remaining = _filter_queue_by_date(project, date(2026, 1, 1))
        assert remaining == 0


class TestForwardFold:
    @patch("engram.bootstrap.fold.next_chunk")
    @patch("engram.bootstrap.fold.build_queue")
    def test_empty_queue_succeeds(
        self, mock_bq: MagicMock, mock_nc: MagicMock, project: Path,
    ) -> None:
        """If no entries after date filter, fold succeeds immediately."""
        mock_bq.return_value = []
        result = forward_fold(project, date(2026, 1, 1))
        assert result is True
        mock_nc.assert_not_called()

    @patch("engram.bootstrap.fold._dispatch_and_validate")
    @patch("engram.bootstrap.fold.next_chunk")
    @patch("engram.bootstrap.fold.build_queue")
    def test_processes_chunks(
        self,
        mock_bq: MagicMock,
        mock_nc: MagicMock,
        mock_dv: MagicMock,
        project: Path,
    ) -> None:
        """Processes chunks until queue exhausted."""
        mock_bq.return_value = [
            {"date": "2026-02-01T00:00:00Z", "type": "doc", "path": "x.md", "chars": 100},
        ]

        # Write queue file so filter has something
        queue_file = project / ".engram" / "queue.jsonl"
        with open(queue_file, "w") as fh:
            fh.write(json.dumps(mock_bq.return_value[0]) + "\n")

        chunk = MagicMock()
        chunk.chunk_id = 1
        chunk.chunk_type = "fold"
        chunk.items_count = 1
        chunk.date_range = "2026-02-01 to 2026-02-01"
        chunk.pre_assigned_ids = {}
        chunk.chunk_chars = 100

        # First call returns chunk, second raises ValueError (empty queue)
        mock_nc.side_effect = [chunk, ValueError("Queue is empty")]
        mock_dv.return_value = True

        result = forward_fold(project, date(2026, 1, 1))
        assert result is True
        mock_dv.assert_called_once()

    @patch("engram.bootstrap.fold._dispatch_and_validate")
    @patch("engram.bootstrap.fold.next_chunk")
    @patch("engram.bootstrap.fold.build_queue")
    def test_reports_failures(
        self,
        mock_bq: MagicMock,
        mock_nc: MagicMock,
        mock_dv: MagicMock,
        project: Path,
    ) -> None:
        """Returns False when a chunk fails."""
        mock_bq.return_value = [
            {"date": "2026-02-01T00:00:00Z", "type": "doc", "path": "x.md", "chars": 100},
        ]
        queue_file = project / ".engram" / "queue.jsonl"
        with open(queue_file, "w") as fh:
            fh.write(json.dumps(mock_bq.return_value[0]) + "\n")

        chunk = MagicMock()
        chunk.chunk_id = 1
        chunk.chunk_type = "fold"
        chunk.items_count = 1
        chunk.date_range = "2026-02-01 to 2026-02-01"
        chunk.pre_assigned_ids = {}
        chunk.chunk_chars = 100

        mock_nc.side_effect = [chunk, ValueError("Queue is empty")]
        mock_dv.return_value = False

        result = forward_fold(project, date(2026, 1, 1))
        assert result is False


# ------------------------------------------------------------------
# CLI tests
# ------------------------------------------------------------------


class TestSeedCLI:
    @patch("engram.bootstrap.seed.seed")
    def test_seed_path_b(self, mock_seed: MagicMock, runner: CliRunner, project: Path) -> None:
        mock_seed.return_value = True
        result = runner.invoke(cli, ["seed", "--project-root", str(project)])
        assert result.exit_code == 0
        assert "Seed complete" in result.output
        mock_seed.assert_called_once()
        # from_date should be None for Path B
        assert mock_seed.call_args[1].get("from_date") is None or mock_seed.call_args[0][1] is None

    @patch("engram.bootstrap.seed.seed")
    def test_seed_path_a(self, mock_seed: MagicMock, runner: CliRunner, project: Path) -> None:
        mock_seed.return_value = True
        result = runner.invoke(
            cli, ["seed", "--project-root", str(project), "--from-date", "2026-01-01"],
        )
        assert result.exit_code == 0
        assert "Seed complete" in result.output

    @patch("engram.bootstrap.seed.seed")
    def test_seed_failure_exit_code(self, mock_seed: MagicMock, runner: CliRunner, project: Path) -> None:
        mock_seed.return_value = False
        result = runner.invoke(cli, ["seed", "--project-root", str(project)])
        assert result.exit_code != 0
        assert "Seed failed" in result.output


class TestFoldCLI:
    @patch("engram.bootstrap.fold.forward_fold")
    def test_fold_success(self, mock_fold: MagicMock, runner: CliRunner, project: Path) -> None:
        mock_fold.return_value = True
        result = runner.invoke(
            cli, ["fold", "--project-root", str(project), "--from", "2026-01-01"],
        )
        assert result.exit_code == 0
        assert "Forward fold complete" in result.output

    @patch("engram.bootstrap.fold.forward_fold")
    def test_fold_failure(self, mock_fold: MagicMock, runner: CliRunner, project: Path) -> None:
        mock_fold.return_value = False
        result = runner.invoke(
            cli, ["fold", "--project-root", str(project), "--from", "2026-01-01"],
        )
        assert result.exit_code != 0
        assert "errors" in result.output

    def test_fold_requires_from(self, runner: CliRunner, project: Path) -> None:
        result = runner.invoke(cli, ["fold", "--project-root", str(project)])
        assert result.exit_code != 0
