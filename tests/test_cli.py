"""Integration tests for engram CLI."""

from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml
import click
from click.testing import CliRunner

from engram.cli import GRAVEYARD_HEADERS, LIVING_DOC_HEADERS, cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Return a fresh tmp dir as project root."""
    return tmp_path


class TestInit:
    def test_creates_engram_dir(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert result.exit_code == 0
        assert (project_dir / ".engram").is_dir()

    def test_creates_config_yaml(self, runner: CliRunner, project_dir: Path) -> None:
        runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        config_path = project_dir / ".engram" / "config.yaml"
        assert config_path.exists()
        config = yaml.safe_load(config_path.read_text())
        assert "living_docs" in config
        assert "graveyard" in config
        assert config["living_docs"]["timeline"] == "docs/decisions/timeline.md"

    def test_creates_all_living_docs(self, runner: CliRunner, project_dir: Path) -> None:
        runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        config = yaml.safe_load((project_dir / ".engram" / "config.yaml").read_text())
        for key, rel_path in config["living_docs"].items():
            doc_path = project_dir / rel_path
            assert doc_path.exists(), f"Missing living doc: {rel_path}"
            content = doc_path.read_text()
            assert content == LIVING_DOC_HEADERS[key]

    def test_creates_graveyard_files(self, runner: CliRunner, project_dir: Path) -> None:
        runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        config = yaml.safe_load((project_dir / ".engram" / "config.yaml").read_text())
        for key, rel_path in config["graveyard"].items():
            doc_path = project_dir / rel_path
            assert doc_path.exists(), f"Missing graveyard: {rel_path}"
            content = doc_path.read_text()
            assert content == GRAVEYARD_HEADERS[key]

    def test_living_doc_headers_have_title(self, runner: CliRunner, project_dir: Path) -> None:
        runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        config = yaml.safe_load((project_dir / ".engram" / "config.yaml").read_text())
        for key, rel_path in config["living_docs"].items():
            content = (project_dir / rel_path).read_text()
            assert content.startswith("# "), f"{key} missing H1 header"

    def test_graveyard_headers_have_title(self, runner: CliRunner, project_dir: Path) -> None:
        runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        config = yaml.safe_load((project_dir / ".engram" / "config.yaml").read_text())
        for key, rel_path in config["graveyard"].items():
            content = (project_dir / rel_path).read_text()
            assert content.startswith("# "), f"{key} graveyard missing H1 header"

    def test_fails_if_engram_dir_exists(self, runner: CliRunner, project_dir: Path) -> None:
        (project_dir / ".engram").mkdir()
        result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert result.exit_code != 0
        assert ".engram/ already exists" in result.output

    def test_config_loads_via_load_config(self, runner: CliRunner, project_dir: Path) -> None:
        """Verify the generated config passes validation through load_config."""
        from engram.config import load_config
        runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        config = load_config(project_dir)
        assert config["living_docs"]["timeline"] == "docs/decisions/timeline.md"
        assert config["model"] == "sonnet"

    def test_output_mentions_all_files(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert "config.yaml" in result.output
        assert "timeline.md" in result.output
        assert "concept_registry.md" in result.output
        assert "epistemic_state.md" in result.output
        assert "workflow_registry.md" in result.output
        assert "concept_graveyard.md" in result.output
        assert "epistemic_graveyard.md" in result.output

    def test_creates_parent_directories(self, runner: CliRunner, project_dir: Path) -> None:
        """init should create intermediate dirs like docs/decisions/."""
        result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert result.exit_code == 0
        assert (project_dir / "docs" / "decisions").is_dir()


class TestMigrateEpistemicHistory:
    def test_externalizes_and_lints(self, runner: CliRunner, project_dir: Path) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        epistemic = project_dir / "docs" / "decisions" / "epistemic_state.md"
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E005: externalize me (believed)\n"
            "**Current position:** true for now.\n"
            "**History:**\n"
            "- 2026-02-21: reviewed\n"
            "**Agent guidance:** keep watching.\n",
        )

        result = runner.invoke(
            cli,
            ["migrate-epistemic-history", "--project-root", str(project_dir)],
        )
        assert result.exit_code == 0
        assert "Epistemic history migration complete." in result.output
        assert "Created current files:" in result.output
        assert "Created history files:" in result.output
        assert "Migrated legacy files:" in result.output
        assert "Lint: PASS" in result.output

        updated = epistemic.read_text()
        assert "**History:**" not in updated
        current_file = project_dir / "docs" / "decisions" / "epistemic_state" / "current" / "E005.md"
        assert current_file.exists()
        history_file = project_dir / "docs" / "decisions" / "epistemic_state" / "history" / "E005.md"
        assert history_file.exists()

    def test_migrates_legacy_per_id_files(self, runner: CliRunner, project_dir: Path) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        legacy_file = project_dir / "docs" / "decisions" / "epistemic_state" / "E005.md"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text("# legacy\n")

        result = runner.invoke(
            cli,
            ["migrate-epistemic-history", "--project-root", str(project_dir)],
        )
        assert result.exit_code == 0
        assert "Migrated legacy files: 1" in result.output
        migrated = project_dir / "docs" / "decisions" / "epistemic_state" / "history" / "E005.md"
        assert migrated.exists()
        assert not legacy_file.exists()


class TestActiveChunkLock:
    def test_next_chunk_blocks_when_active_lock_present(self, runner: CliRunner, project_dir: Path) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        # Create docs referenced by the queue
        a = project_dir / "docs" / "working" / "a.md"
        b = project_dir / "docs" / "working" / "b.md"
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_text("# A\n")
        b.write_text("# B\n")

        queue_file = project_dir / ".engram" / "queue.jsonl"
        entries = [
            {"date": "2026-01-01T00:00:00", "type": "doc", "path": "docs/working/a.md", "chars": 150000, "pass": "initial"},
            {"date": "2026-01-02T00:00:00", "type": "doc", "path": "docs/working/b.md", "chars": 150000, "pass": "initial"},
        ]
        queue_file.write_text("".join(json.dumps(e) + "\n" for e in entries))

        # First generation should succeed and write lock
        r1 = runner.invoke(cli, ["next-chunk", "--project-root", str(project_dir)])
        assert r1.exit_code == 0
        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        assert lock_path.exists()

        # Second generation should fail fast due to active lock
        r2 = runner.invoke(cli, ["next-chunk", "--project-root", str(project_dir)])
        assert r2.exit_code != 0
        assert "Active chunk lock present" in r2.output

        # Clearing the lock should allow generating the next chunk
        rc = runner.invoke(cli, ["clear-active-chunk", "--project-root", str(project_dir)])
        assert rc.exit_code == 0
        assert not lock_path.exists()

        r3 = runner.invoke(cli, ["next-chunk", "--project-root", str(project_dir)])
        assert r3.exit_code == 0

    def test_auto_clear_does_not_unlock_on_older_matching_commit(self, runner: CliRunner, project_dir: Path) -> None:
        import os
        import subprocess

        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        # Make project_dir a git repo with an OLD commit that matches the chunk subject pattern.
        subprocess.run(["git", "init"], cwd=str(project_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(project_dir), check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(project_dir), check=True)
        (project_dir / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(project_dir), check=True)
        old_env = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
        }
        subprocess.run(
            ["git", "commit", "-m", "Knowledge fold: chunk 1 (old)"],
            cwd=str(project_dir),
            check=True,
            capture_output=True,
            env=old_env,
        )

        # Create docs referenced by the queue
        a = project_dir / "docs" / "working" / "a.md"
        b = project_dir / "docs" / "working" / "b.md"
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_text("# A\n")
        b.write_text("# B\n")

        queue_file = project_dir / ".engram" / "queue.jsonl"
        entries = [
            {"date": "2026-01-01T00:00:00", "type": "doc", "path": "docs/working/a.md", "chars": 150000, "pass": "initial"},
            {"date": "2026-01-02T00:00:00", "type": "doc", "path": "docs/working/b.md", "chars": 150000, "pass": "initial"},
        ]
        queue_file.write_text("".join(json.dumps(e) + "\n" for e in entries))

        # First generation writes the active lock (chunk_id=1).
        r1 = runner.invoke(cli, ["next-chunk", "--project-root", str(project_dir)])
        assert r1.exit_code == 0
        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        assert lock_path.exists()

        # Second generation should NOT auto-clear (the matching commit is older than the lock).
        r2 = runner.invoke(cli, ["next-chunk", "--project-root", str(project_dir)])
        assert r2.exit_code != 0
        assert "Active chunk lock present" in r2.output
        assert lock_path.exists()

    def test_invalid_active_chunk_metadata_blocks_generation(self, runner: CliRunner, project_dir: Path) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        lock_path.write_text("chunk_id: not-an-int\ncreated_at: 2026-01-01T00:00:00Z\n")

        result = runner.invoke(cli, ["next-chunk", "--project-root", str(project_dir)])
        assert result.exit_code != 0
        assert "Active chunk lock metadata is invalid" in result.output

        generation_lock = project_dir / ".engram" / "active_chunk.lock"
        assert not generation_lock.exists()

    def test_clear_active_chunk_removes_context_worktree(self, runner: CliRunner, project_dir: Path) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        context_dir = Path(tempfile.mkdtemp(prefix="engram-chunk-001-abcdef12-"))
        (context_dir / "marker.txt").write_text("x")

        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        lock_path.write_text(
            yaml.safe_dump(
                {
                    "chunk_id": 1,
                    "created_at": "2026-01-01T00:00:00Z",
                    "context_worktree_path": str(context_dir),
                },
                sort_keys=True,
            ),
        )

        result = runner.invoke(cli, ["clear-active-chunk", "--project-root", str(project_dir)])
        assert result.exit_code == 0
        assert not lock_path.exists()
        assert not context_dir.exists()

    def test_clear_active_chunk_does_not_remove_non_engram_temp_paths(self, runner: CliRunner, project_dir: Path) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        unrelated = Path(tempfile.mkdtemp(prefix="unrelated-temp-dir-"))
        (unrelated / "marker.txt").write_text("x")

        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        lock_path.write_text(
            yaml.safe_dump(
                {
                    "chunk_id": 1,
                    "created_at": "2026-01-01T00:00:00Z",
                    "context_worktree_path": str(unrelated),
                },
                sort_keys=True,
            ),
        )

        result = runner.invoke(cli, ["clear-active-chunk", "--project-root", str(project_dir)])
        assert result.exit_code == 0
        assert unrelated.exists()

    def test_clear_active_chunk_removes_context_worktree_for_chunk_id_1000(self, runner: CliRunner, project_dir: Path) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        context_dir = Path(tempfile.mkdtemp(prefix="engram-chunk-1000-abcdef12-"))
        (context_dir / "marker.txt").write_text("x")

        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        lock_path.write_text(
            yaml.safe_dump(
                {
                    "chunk_id": 1000,
                    "created_at": "2026-01-01T00:00:00Z",
                    "context_worktree_path": str(context_dir),
                },
                sort_keys=True,
            ),
        )

        result = runner.invoke(cli, ["clear-active-chunk", "--project-root", str(project_dir)])
        assert result.exit_code == 0
        assert not context_dir.exists()

    def test_generation_lock_is_held_during_next_chunk(self, runner: CliRunner, project_dir: Path, monkeypatch) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        observed = {"lock_seen": False}

        def fake_next_chunk(config, root, fold_from=None):  # noqa: ANN001
            observed["lock_seen"] = (root / ".engram" / "active_chunk.lock").exists()
            chunks_dir = root / ".engram" / "chunks"
            chunks_dir.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(
                chunk_id=1,
                chunk_type="fold",
                living_docs_chars=0,
                budget=1000,
                items_count=0,
                chunk_chars=0,
                date_range="2026-01-01 to 2026-01-01",
                pre_assigned_ids={},
                input_path=chunks_dir / "chunk_001_input.md",
                prompt_path=chunks_dir / "chunk_001_prompt.txt",
                remaining_queue=0,
                drift_entry_count=0,
            )

        import engram.fold.chunker as chunker_module

        monkeypatch.setattr(chunker_module, "next_chunk", fake_next_chunk)
        result = runner.invoke(cli, ["next-chunk", "--project-root", str(project_dir)])
        assert result.exit_code == 0
        assert observed["lock_seen"] is True
        assert not (project_dir / ".engram" / "active_chunk.lock").exists()

    def test_next_chunk_ensures_lock_files_are_gitignored(self, runner: CliRunner, project_dir: Path) -> None:
        init_result = runner.invoke(cli, ["init", "--project-root", str(project_dir)])
        assert init_result.exit_code == 0

        queue_file = project_dir / ".engram" / "queue.jsonl"
        queue_file.write_text(
            json.dumps(
                {
                    "date": "2026-01-01T00:00:00",
                    "type": "doc",
                    "path": "docs/working/a.md",
                    "chars": 100,
                    "pass": "initial",
                },
            ) + "\n",
        )
        doc = project_dir / "docs" / "working" / "a.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# A\n")

        result = runner.invoke(cli, ["next-chunk", "--project-root", str(project_dir)])
        assert result.exit_code == 0

        gitignore = project_dir / ".engram" / ".gitignore"
        text = gitignore.read_text()
        assert "active_chunk.yaml" in text
        assert "active_chunk.lock" in text

    def test_auto_clear_includes_same_second_commit_timestamp(self, project_dir: Path, monkeypatch) -> None:
        from datetime import datetime, timezone
        import subprocess
        from engram import cli as cli_module

        (project_dir / ".engram").mkdir(parents=True, exist_ok=True)
        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        created_at = "2026-01-01T00:00:05Z"
        created_epoch = int(
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp(),
        )
        lock_path.write_text(
            yaml.safe_dump(
                {
                    "chunk_id": 12,
                    "created_at": created_at,
                    "input_path": "dummy.md",
                },
                sort_keys=True,
            ),
        )

        class DummyProc:
            def __init__(self, returncode: int = 0, stdout: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout

        def fake_run(args, cwd=None, capture_output=False, text=False, check=False):  # noqa: ANN001
            if args[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                return DummyProc(returncode=0, stdout="true\n")
            if args[:4] == ["git", "log", "-n", "200"]:
                # Same second as created_at; should be included and clear the lock.
                return DummyProc(returncode=0, stdout=f"{created_epoch}\tKnowledge fold: chunk 12\n")
            raise AssertionError(f"Unexpected subprocess args: {args}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        cli_module._enforce_single_active_chunk(project_dir)
        assert not lock_path.exists()

    def test_auto_clear_accepts_fold_chunk_commit_subject(self, project_dir: Path, monkeypatch) -> None:
        from datetime import datetime, timezone
        import subprocess
        from engram import cli as cli_module

        (project_dir / ".engram").mkdir(parents=True, exist_ok=True)
        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        created_at = "2026-01-01T00:00:05Z"
        created_epoch = int(
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp(),
        )
        lock_path.write_text(
            yaml.safe_dump(
                {
                    "chunk_id": 34,
                    "created_at": created_at,
                    "input_path": "dummy.md",
                },
                sort_keys=True,
            ),
        )

        class DummyProc:
            def __init__(self, returncode: int = 0, stdout: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout

        def fake_run(args, cwd=None, capture_output=False, text=False, check=False):  # noqa: ANN001
            if args[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                return DummyProc(returncode=0, stdout="true\n")
            if args[:4] == ["git", "log", "-n", "200"]:
                return DummyProc(returncode=0, stdout=f"{created_epoch}\tFold chunk 34: details\n")
            raise AssertionError(f"Unexpected subprocess args: {args}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        cli_module._enforce_single_active_chunk(project_dir)
        assert not lock_path.exists()

    def test_auto_clear_does_not_match_fold_chunk_substring(self, project_dir: Path, monkeypatch) -> None:
        from datetime import datetime, timezone
        import subprocess
        from engram import cli as cli_module

        (project_dir / ".engram").mkdir(parents=True, exist_ok=True)
        lock_path = project_dir / ".engram" / "active_chunk.yaml"
        created_at = "2026-01-01T00:00:05Z"
        created_epoch = int(
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp(),
        )
        lock_path.write_text(
            yaml.safe_dump(
                {
                    "chunk_id": 34,
                    "created_at": created_at,
                    "input_path": "dummy.md",
                },
                sort_keys=True,
            ),
        )

        class DummyProc:
            def __init__(self, returncode: int = 0, stdout: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout

        def fake_run(args, cwd=None, capture_output=False, text=False, check=False):  # noqa: ANN001
            if args[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                return DummyProc(returncode=0, stdout="true\n")
            if args[:4] == ["git", "log", "-n", "200"]:
                return DummyProc(returncode=0, stdout=f"{created_epoch}\tScaffold chunk 34 cleanup\n")
            raise AssertionError(f"Unexpected subprocess args: {args}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(click.ClickException, match="Active chunk lock present"):
            cli_module._enforce_single_active_chunk(project_dir)
        assert lock_path.exists()
