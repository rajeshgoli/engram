"""Integration tests for engram CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
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
        assert "Lint: PASS" in result.output

        updated = epistemic.read_text()
        assert "**History:**" not in updated
        history_file = project_dir / "docs" / "decisions" / "epistemic_state" / "E005.md"
        assert history_file.exists()


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
