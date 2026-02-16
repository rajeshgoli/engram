"""Integration tests for engram CLI."""

from __future__ import annotations

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
