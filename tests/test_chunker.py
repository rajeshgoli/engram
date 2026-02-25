"""Tests for chunk assembly and drift-priority scheduling."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engram.fold.chunker import (
    _QUEUE_TEXT_CACHE,
    ChunkResult,
    DriftReport,
    _collect_context_pack,
    _extract_code_paths,
    _extract_latest_date,
    _find_claims_by_status,
    _find_orphaned_concepts,
    _find_stale_epistemic_entries,
    _find_workflow_repetitions,
    _living_docs_char_counts,
    _predict_touched_ids,
    _resolve_git_line_commit_date,
    _read_queue_entry_text,
    _render_item_content,
    cleanup_chunk_context_worktree,
    compute_budget,
    next_chunk,
    scan_drift,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    """Set up a minimal engram project with config and living docs."""
    engram_dir = tmp_path / ".engram"
    engram_dir.mkdir()

    # Write config
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
  orphan_triage: 3
  contested_review_days: 14
  stale_unverified_days: 30
  stale_epistemic_days: 90
  workflow_repetition: 3
budget:
  context_limit_chars: 600000
  instructions_overhead: 10000
  max_chunk_chars: 200000
"""
    (engram_dir / "config.yaml").write_text(config_yaml)

    # Create living doc directories
    docs_dir = tmp_path / "docs" / "decisions"
    docs_dir.mkdir(parents=True)

    # Create empty living docs
    (docs_dir / "timeline.md").write_text("# Timeline\n")
    (docs_dir / "concept_registry.md").write_text("# Concept Registry\n")
    (docs_dir / "epistemic_state.md").write_text("# Epistemic State\n")
    (docs_dir / "workflow_registry.md").write_text("# Workflow Registry\n")
    (docs_dir / "concept_graveyard.md").write_text("# Concept Graveyard\n")
    (docs_dir / "epistemic_graveyard.md").write_text("# Epistemic Graveyard\n")

    return tmp_path


@pytest.fixture()
def config(project):
    """Load config from the project fixture."""
    from engram.config import load_config
    return load_config(project)


def _write_queue(project, items):
    """Write queue.jsonl to .engram/."""
    queue_file = project / ".engram" / "queue.jsonl"
    with open(queue_file, "w") as fh:
        for item in items:
            fh.write(json.dumps(item) + "\n")


def _make_doc_item(path="docs/working/spec.md", chars=500, date="2025-01-15T00:00:00"):
    """Create a minimal doc queue entry."""
    return {
        "date": date,
        "type": "doc",
        "path": path,
        "chars": chars,
        "pass": "initial",
    }


# ------------------------------------------------------------------
# DriftReport.triggered
# ------------------------------------------------------------------


class TestDriftReportTriggered:
    def test_no_drift(self):
        report = DriftReport()
        assert report.triggered({"orphan_triage": 50}) is None

    def test_orphan_triage_triggered(self):
        report = DriftReport(
            orphaned_concepts=[{"name": f"c{i}"} for i in range(51)]
        )
        assert report.triggered({"orphan_triage": 50}) == "orphan_triage"

    def test_orphan_triage_at_threshold_not_triggered(self):
        report = DriftReport(
            orphaned_concepts=[{"name": f"c{i}"} for i in range(50)]
        )
        assert report.triggered({"orphan_triage": 50}) is None

    def test_epistemic_audit_triggered(self):
        report = DriftReport(
            epistemic_audit=[{"name": "E001"}],
        )
        assert report.triggered({}) == "epistemic_audit"

    def test_epistemic_audit_at_threshold_not_triggered(self):
        report = DriftReport(
            epistemic_audit=[{"name": "E001"}],
        )
        assert report.triggered({"epistemic_audit": 1}) is None

    def test_contested_review_triggered(self):
        report = DriftReport(
            contested_claims=[{"name": f"claim{i}", "days_old": 20} for i in range(6)]
        )
        assert report.triggered({"contested_review": 5}) == "contested_review"

    def test_contested_review_at_threshold_not_triggered(self):
        report = DriftReport(
            contested_claims=[{"name": f"claim{i}", "days_old": 20} for i in range(5)]
        )
        assert report.triggered({"contested_review": 5}) is None

    def test_stale_unverified_triggered(self):
        report = DriftReport(
            stale_unverified=[{"name": f"claim{i}", "days_old": 35} for i in range(11)]
        )
        assert report.triggered({"stale_unverified": 10}) == "stale_unverified"

    def test_stale_unverified_at_threshold_not_triggered(self):
        report = DriftReport(
            stale_unverified=[{"name": f"claim{i}", "days_old": 35} for i in range(10)]
        )
        assert report.triggered({"stale_unverified": 10}) is None

    def test_workflow_synthesis_triggered(self):
        report = DriftReport(
            workflow_repetitions=[{"name": f"w{i}"} for i in range(4)]
        )
        assert report.triggered({"workflow_repetition": 3}) == "workflow_synthesis"

    def test_priority_order_orphan_over_contested(self):
        report = DriftReport(
            orphaned_concepts=[{"name": f"c{i}"} for i in range(4)],
            contested_claims=[{"name": f"claim{i}"} for i in range(6)],
        )
        assert report.triggered({"orphan_triage": 3, "contested_review": 5}) == "orphan_triage"

    def test_priority_order_epistemic_over_contested(self):
        report = DriftReport(
            epistemic_audit=[{"name": "E001"}],
            contested_claims=[{"name": f"claim{i}"} for i in range(6)],
        )
        assert report.triggered({"contested_review": 5}) == "epistemic_audit"

    def test_priority_order_contested_over_stale(self):
        report = DriftReport(
            contested_claims=[{"name": f"c{i}"} for i in range(6)],
            stale_unverified=[{"name": f"s{i}"} for i in range(11)],
        )
        assert report.triggered({"contested_review": 5, "stale_unverified": 10}) == "contested_review"


# ------------------------------------------------------------------
# Code path extraction
# ------------------------------------------------------------------


class TestExtractCodePaths:
    def test_single_path(self):
        text = "## C001: foo (ACTIVE)\n- **Code:** `src/foo.py`\n"
        assert _extract_code_paths(text) == ["src/foo.py"]

    def test_multiple_comma_separated(self):
        text = "- **Code:** `src/a.py`, `src/b.py`\n"
        assert _extract_code_paths(text) == ["src/a.py", "src/b.py"]

    def test_no_backticks(self):
        text = "- **Code:** src/foo.py\n"
        assert _extract_code_paths(text) == ["src/foo.py"]

    def test_no_code_field(self):
        text = "## C001: foo (ACTIVE)\n- **Issues:** #42\n"
        assert _extract_code_paths(text) == []

    def test_expands_brace_paths(self):
        text = "- **Code:** docs/archive/{a,b}.md, src/router/{x, y}.py\n"
        assert _extract_code_paths(text) == [
            "docs/archive/a.md",
            "docs/archive/b.md",
            "src/router/x.py",
            "src/router/y.py",
        ]

    def test_splits_top_level_commas_only(self):
        text = "- **Code:** src/replay_server/routers/{annotate,user,screenshots,dag}.py, scripts/run.py\n"
        assert _extract_code_paths(text) == [
            "src/replay_server/routers/annotate.py",
            "src/replay_server/routers/user.py",
            "src/replay_server/routers/screenshots.py",
            "src/replay_server/routers/dag.py",
            "scripts/run.py",
        ]


# ------------------------------------------------------------------
# Orphan detection
# ------------------------------------------------------------------


class TestFindOrphanedConcepts:
    def test_finds_orphans(self, project):
        registry = project / "docs" / "decisions" / "concept_registry.md"
        registry.write_text(
            "# Concept Registry\n\n"
            "## C001: real_module (ACTIVE)\n"
            "- **Code:** `src/missing.py`\n\n"
            "## C002: present_module (ACTIVE)\n"
            "- **Code:** `src/present.py`\n"
        )
        # Create only the present file
        (project / "src").mkdir()
        (project / "src" / "present.py").write_text("")

        orphans = _find_orphaned_concepts(registry, project)
        assert len(orphans) == 1
        assert orphans[0]["id"] == "C001"
        assert "src/missing.py" in orphans[0]["paths"]

    def test_skips_dead_entries(self, project):
        registry = project / "docs" / "decisions" / "concept_registry.md"
        registry.write_text(
            "# Concept Registry\n\n"
            "## C001: dead_concept (DEAD) \u2192 concept_graveyard.md#C001\n"
        )
        orphans = _find_orphaned_concepts(registry, project)
        assert orphans == []

    def test_skips_entries_without_code(self, project):
        registry = project / "docs" / "decisions" / "concept_registry.md"
        registry.write_text(
            "# Concept Registry\n\n"
            "## C001: abstract_concept (ACTIVE)\n"
            "- **Issues:** #5\n"
        )
        orphans = _find_orphaned_concepts(registry, project)
        assert orphans == []

    def test_empty_registry(self, project):
        registry = project / "docs" / "decisions" / "concept_registry.md"
        registry.write_text("# Concept Registry\n")
        orphans = _find_orphaned_concepts(registry, project)
        assert orphans == []


# ------------------------------------------------------------------
# Date extraction
# ------------------------------------------------------------------


class TestExtractLatestDate:
    def test_single_date(self):
        text = "- #42: 'some claim' \u2192 contested (2025-06-15)"
        dt = _extract_latest_date(text)
        assert dt is not None
        assert dt.strftime("%Y-%m-%d") == "2025-06-15"

    def test_multiple_dates_returns_latest(self):
        text = (
            "- 2025-01-10: first claim\n"
            "- 2025-06-20: updated claim\n"
            "- 2025-03-15: mid claim\n"
        )
        dt = _extract_latest_date(text)
        assert dt is not None
        assert dt.strftime("%Y-%m-%d") == "2025-06-20"

    def test_no_dates(self):
        assert _extract_latest_date("no dates here") is None

    def test_month_day_without_year_uses_nearest_past(self):
        target = datetime.now(timezone.utc) - timedelta(days=120)
        text = f"- Product {target.strftime('%b %d')}: evidence updated"
        dt = _extract_latest_date(text)
        assert dt is not None
        assert dt <= datetime.now(timezone.utc)
        assert (datetime.now(timezone.utc) - dt).days >= 90

    def test_month_day_with_year_is_parsed(self):
        text = "- Product Jan 05, 2025: evidence updated"
        dt = _extract_latest_date(text)
        assert dt is not None
        assert dt.strftime("%Y-%m-%d") == "2025-01-05"

    def test_month_day_year_without_comma_is_parsed(self):
        text = "- Product Jan 05 2010: evidence updated"
        dt = _extract_latest_date(text)
        assert dt is not None
        assert dt.strftime("%Y-%m-%d") == "2010-01-05"

    def test_day_month_year_is_parsed(self):
        text = "- Product 11 Dec, 2025: evidence updated"
        dt = _extract_latest_date(text)
        assert dt is not None
        assert dt.strftime("%Y-%m-%d") == "2025-12-11"

    def test_day_month_year_without_comma_is_parsed(self):
        text = "- Product 11 Dec 2010: evidence updated"
        dt = _extract_latest_date(text)
        assert dt is not None
        assert dt.strftime("%Y-%m-%d") == "2010-12-11"


# ------------------------------------------------------------------
# Contested / unverified claim detection
# ------------------------------------------------------------------


class TestFindClaimsByStatus:
    def test_finds_old_contested(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            f"## E001: some_claim (contested)\n"
            f"**History:**\n"
            f"- #{old_date}: 'initial' \u2192 contested\n"
        )
        results = _find_claims_by_status(epistemic, "contested", 14)
        assert len(results) == 1
        assert results[0]["id"] == "E001"
        assert results[0]["days_old"] >= 20

    def test_ignores_recent_contested(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            f"## E001: new_claim (contested)\n"
            f"**History:**\n"
            f"- {recent_date}: 'initial'\n"
        )
        results = _find_claims_by_status(epistemic, "contested", 14)
        assert results == []

    def test_finds_stale_unverified(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            f"## E002: stale_claim (unverified)\n"
            f"**History:**\n"
            f"- {old_date}: first noted\n"
        )
        results = _find_claims_by_status(epistemic, "unverified", 30)
        assert len(results) == 1
        assert results[0]["id"] == "E002"

    def test_skips_entries_without_dates(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E001: no_date_claim (contested)\n"
            "**Current position:** Unknown.\n"
        )
        results = _find_claims_by_status(epistemic, "contested", 14)
        assert results == []

    def test_uses_external_history_when_inline_missing(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E021: contested external history (contested)\n"
            "**Current position:** disputed.\n"
        )
        history_file = (
            project / "docs" / "decisions" / "epistemic_state" / "history" / "E021.md"
        )
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(
            "# Epistemic History\n\n"
            "## E021: contested external history\n\n"
            f"- {old_date}: reopened\n",
        )
        results = _find_claims_by_status(epistemic, "contested", 14)
        assert len(results) == 1
        assert results[0]["id"] == "E021"

    def test_recent_heading_commit_suppresses_old_contested_requeue(self, project):
        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)

        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")

        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E001: callback parity baseline (believed)\n"
            "**History:**\n"
            f"- {old_date}: legacy investigation\n",
        )
        subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial believed state"],
            cwd=project,
            check=True,
            capture_output=True,
        )

        # Flip status now, but keep old dated history text.
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E001: callback parity baseline (contested)\n"
            "**History:**\n"
            f"- {old_date}: legacy investigation\n",
        )
        subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "flip claim to contested"],
            cwd=project,
            check=True,
            capture_output=True,
        )

        results = _find_claims_by_status(
            epistemic,
            "contested",
            14,
            project_root=project,
        )
        assert results == []

    def test_heading_blame_cache_invalidates_when_file_changes(self, project, monkeypatch):
        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)

        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E001: callback parity baseline (contested)\n"
            "**Current position:** initial wording.\n",
        )
        subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial contested heading"],
            cwd=project,
            check=True,
            capture_output=True,
        )

        import engram.fold.chunker as chunker_module

        chunker_module._HEADING_LINE_COMMIT_DATE_CACHE.clear()
        original_run = chunker_module.subprocess.run
        blame_calls = 0

        def counting_run(*args, **kwargs):
            nonlocal blame_calls
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and "blame" in cmd:
                blame_calls += 1
            return original_run(*args, **kwargs)

        monkeypatch.setattr(chunker_module.subprocess, "run", counting_run)

        first = _resolve_git_line_commit_date(
            project_root=project,
            file_path=epistemic,
            line_number_1based=3,
        )
        assert first is not None
        assert blame_calls == 1

        time.sleep(1.1)
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E001: callback parity baseline (contested)\n"
            "**Current position:** updated wording after review.\n",
        )
        subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "update contested entry wording"],
            cwd=project,
            check=True,
            capture_output=True,
        )

        second = _resolve_git_line_commit_date(
            project_root=project,
            file_path=epistemic,
            line_number_1based=3,
        )
        assert second is not None
        assert blame_calls == 2
        assert second >= first

    def test_heading_blame_cache_invalidates_when_head_changes_only(self, project, monkeypatch):
        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)

        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E001: callback parity baseline (contested)\n"
            "**Current position:** staged for first commit.\n",
        )

        import engram.fold.chunker as chunker_module

        chunker_module._HEADING_LINE_COMMIT_DATE_CACHE.clear()
        original_run = chunker_module.subprocess.run
        blame_calls = 0

        def counting_run(*args, **kwargs):
            nonlocal blame_calls
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and "blame" in cmd:
                blame_calls += 1
            return original_run(*args, **kwargs)

        monkeypatch.setattr(chunker_module.subprocess, "run", counting_run)

        # File is uncommitted; blame should fail and cache None.
        first = _resolve_git_line_commit_date(
            project_root=project,
            file_path=epistemic,
            line_number_1based=3,
        )
        assert first is None
        assert blame_calls == 1

        # Commit without changing file bytes.
        subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add epistemic file"],
            cwd=project,
            check=True,
            capture_output=True,
        )

        second = _resolve_git_line_commit_date(
            project_root=project,
            file_path=epistemic,
            line_number_1based=3,
        )
        assert second is not None
        # Must re-run blame after HEAD change even when mtime/size key parts are unchanged.
        assert blame_calls == 2


class TestFindStaleEpistemicEntries:
    def test_queue_text_cache_avoids_reread(self, project, monkeypatch):
        _QUEUE_TEXT_CACHE.clear()

        doc = project / "docs" / "working" / "cache_test.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Harness phase 0 completion detail")
        item = {
            "date": datetime.now(timezone.utc).isoformat(),
            "type": "doc",
            "path": "docs/working/cache_test.md",
            "chars": 100,
            "pass": "initial",
        }

        first = _read_queue_entry_text(project, item)
        assert "harness phase 0 completion detail" in first

        def fail_read_text(self, *args, **kwargs):
            raise AssertionError("expected cached queue text")

        monkeypatch.setattr(Path, "read_text", fail_read_text)
        second = _read_queue_entry_text(project, item)
        assert second == first

    def test_finds_stale_believed_entry(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E008: harness phase 0 completion (believed)\n"
            "**History:**\n"
            f"- {old_date}: backlog noted\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E008"
        assert results[0]["status"] == "believed"

    def test_evidence_commit_in_external_history_advances_activity_date(self, project):
        if shutil.which("git") is None:
            pytest.skip("git is required for Evidence@<commit> staleness test")

        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
        (project / "README.md").write_text("test\n")
        subprocess.run(["git", "add", "README.md"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E087: inner structure pruning impact (believed)\n"
            "**History:**\n"
            f"- {old_date}: initial claim\n"
        )

        history_dir = project / "docs" / "decisions" / "epistemic_state" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / "E087.md").write_text(
            "## E087: inner structure pruning impact\n"
            f"- Evidence@{sha} docs/decisions/epistemic_state.md:1: audit update -> believed\n"
        )

        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
            project_root=project,
            queue_entries=[],
        )
        assert results == []

    def test_unresolvable_evidence_commit_does_not_crash(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E088: some claim (believed)\n"
            "**History:**\n"
            f"- {old_date}: initial claim\n"
        )

        history_dir = project / "docs" / "decisions" / "epistemic_state" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / "E088.md").write_text(
            "## E088: some claim\n"
            "- Evidence@deadbeef docs/decisions/epistemic_state.md:1: unknown -> believed\n"
        )

        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
            project_root=project,
            queue_entries=[],
        )
        assert len(results) == 1
        assert results[0]["id"] == "E088"

    def test_threshold_behavior_age_equal_not_stale(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        exact_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E010: recent enough claim (unverified)\n"
            "**History:**\n"
            f"- {exact_date}: noted\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=30,
        )
        assert results == []

    def test_queue_reference_since_history_suppresses(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E011: harness phase 0 completion (believed)\n"
            "**History:**\n"
            f"- {old_date}: backlog noted\n"
        )

        note_path = project / "docs" / "working" / "harness_update.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("Harness phase 0 completion is now tracked elsewhere.")

        queue_entries = [
            {
                "date": datetime.now(timezone.utc).isoformat(),
                "type": "doc",
                "path": "docs/working/harness_update.md",
                "chars": 100,
                "pass": "initial",
            },
        ]
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
            project_root=project,
            queue_entries=queue_entries,
        )
        assert results == []

    def test_queue_reference_before_history_does_not_suppress(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        history_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        old_queue_date = (datetime.now(timezone.utc) - timedelta(days=150)).isoformat()
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E012: harness phase 0 completion (believed)\n"
            "**History:**\n"
            f"- {history_date}: backlog noted\n"
        )

        note_path = project / "docs" / "working" / "old_note.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("harness phase 0 completion old context")

        queue_entries = [
            {
                "date": old_queue_date,
                "type": "doc",
                "path": "docs/working/old_note.md",
                "chars": 100,
                "pass": "initial",
            },
        ]
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
            project_root=project,
            queue_entries=queue_entries,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E012"

    def test_issue_title_reference_since_history_suppresses(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E018: harness phase 0 completion (believed)\n"
            "**History:**\n"
            f"- {old_date}: backlog noted\n"
        )

        issue_dir = project / "local_data" / "issues"
        issue_dir.mkdir(parents=True, exist_ok=True)
        issue_data = {
            "number": 99,
            "title": "harness phase 0 completion follow-up",
            "body": "",
            "state": "open",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "labels": [],
            "comments": [],
        }
        (issue_dir / "99.json").write_text(json.dumps(issue_data))

        queue_entries = [
            {
                "date": datetime.now(timezone.utc).isoformat(),
                "type": "issue",
                "path": "local_data/issues/99.json",
                "chars": 100,
                "pass": "initial",
                "issue_number": 99,
                "issue_title": "harness phase 0 completion follow-up",
            },
        ]
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
            project_root=project,
            queue_entries=queue_entries,
        )
        assert results == []

    def test_post_history_field_date_does_not_override_history_date(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E013: parser boundary check (believed)\n"
            "**History:**\n"
            f"- {old_date}: original claim\n"
            f"**Agent guidance:** recheck after {recent_date}\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E013"

    def test_bullet_prefixed_history_field_is_parsed(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E014: bullet history format (unverified)\n"
            "- **History:**\n"
            f"- {old_date}: first noted\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E014"

    def test_plain_history_field_is_parsed(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E015: plain history format (believed)\n"
            "History:\n"
            f"- {old_date}: first noted\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E015"

    def test_bullet_prefixed_plain_history_field_is_parsed(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E017: bullet plain history format (believed)\n"
            "- History:\n"
            f"- {old_date}: first noted\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E017"

    def test_plain_post_history_field_date_does_not_override_history_date(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E016: plain field boundary check (believed)\n"
            "History:\n"
            f"- {old_date}: original claim\n"
            f"Agent guidance: recheck after {recent_date}\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E016"

    def test_non_iso_history_date_is_stale_candidate(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_human_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%b %d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E019: harness phase 0 completion (believed)\n"
            "**History:**\n"
            f"- Product {old_human_date}: moved to backlog\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E019"

    def test_history_line_with_colon_is_not_treated_as_new_field(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_human_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%b %d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E020: harness phase 0 completion (believed)\n"
            "**History:**\n"
            f"- Product {old_human_date}: moved to backlog\n"
        )
        results = _find_stale_epistemic_entries(
            epistemic,
            days_threshold=90,
        )
        assert len(results) == 1
        assert results[0]["id"] == "E020"

    def test_uses_external_history_when_inline_missing(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E022: external history only (believed)\n"
            "**Current position:** still believed.\n"
        )
        history_file = (
            project / "docs" / "decisions" / "epistemic_state" / "history" / "E022.md"
        )
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(
            "# Epistemic History\n\n"
            "## E022: external history only\n\n"
            f"- {old_date}: reviewed\n",
        )
        results = _find_stale_epistemic_entries(epistemic, days_threshold=90)
        assert len(results) == 1
        assert results[0]["id"] == "E022"

    def test_external_history_newer_than_inline_suppresses_stale(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E023: mixed history freshness (believed)\n"
            "**History:**\n"
            f"- {old_date}: old inline\n"
        )
        history_file = (
            project / "docs" / "decisions" / "epistemic_state" / "history" / "E023.md"
        )
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(
            "# Epistemic History\n\n"
            "## E023: mixed history freshness\n\n"
            f"- {recent_date}: latest external\n",
        )
        results = _find_stale_epistemic_entries(epistemic, days_threshold=90)
        assert results == []

    def test_current_file_newer_than_inline_suppresses_stale(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E089: split current freshness (believed)\n"
            "**History:**\n"
            f"- {old_date}: stale inline\n",
        )
        current_file = (
            project / "docs" / "decisions" / "epistemic_state" / "current" / "E089.md"
        )
        current_file.parent.mkdir(parents=True, exist_ok=True)
        current_file.write_text(
            "## E089: split current freshness (believed)\n"
            f"**Current position:** Validated again on {recent_date}.\n"
            "**Agent guidance:** keep monitoring.\n",
        )
        results = _find_stale_epistemic_entries(epistemic, days_threshold=90)
        assert results == []

    def test_external_history_ignores_other_entry_sections(self, project):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E024: scoped external history (believed)\n"
            "**Current position:** still believed.\n"
        )
        history_file = (
            project / "docs" / "decisions" / "epistemic_state" / "history" / "E024.md"
        )
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(
            "# Epistemic History\n\n"
            "## E999: unrelated entry\n\n"
            f"- {recent_date}: unrelated fresh update\n\n"
            "## E024: scoped external history\n\n"
            f"- {old_date}: stale update for target claim\n",
        )
        results = _find_stale_epistemic_entries(epistemic, days_threshold=90)
        assert len(results) == 1
        assert results[0]["id"] == "E024"


# ------------------------------------------------------------------
# Workflow repetition detection
# ------------------------------------------------------------------


class TestFindWorkflowRepetitions:
    def test_finds_current_workflows(self, project):
        workflows = project / "docs" / "decisions" / "workflow_registry.md"
        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n"
            "- **Context:** production deploys\n\n"
            "## W002: review_process (CURRENT)\n"
            "- **Context:** code review\n\n"
            "## W003: old_process (SUPERSEDED) \u2192 W001\n"
        )
        results = _find_workflow_repetitions(workflows)
        assert len(results) == 2
        assert results[0]["id"] == "W001"
        assert results[1]["id"] == "W002"

    def test_empty_workflows(self, project):
        workflows = project / "docs" / "decisions" / "workflow_registry.md"
        workflows.write_text("# Workflow Registry\n")
        assert _find_workflow_repetitions(workflows) == []


# ------------------------------------------------------------------
# Budget computation
# ------------------------------------------------------------------


class TestComputeBudget:
    def test_basic_budget(self, project, config):
        from engram.config import resolve_doc_paths
        paths = resolve_doc_paths(config, project)
        budget, living_chars = compute_budget(config, paths)

        # With minimal docs (~100 chars total), budget should be near max_chunk
        assert budget <= 200_000
        assert budget > 0
        assert living_chars > 0

    def test_large_docs_reduce_budget(self, project, config):
        from engram.config import resolve_doc_paths
        paths = resolve_doc_paths(config, project)

        # Write a large timeline
        timeline = paths["timeline"]
        timeline.write_text("x" * 300_000)

        budget, living_chars = compute_budget(config, paths)
        assert living_chars >= 300_000
        # Budget = 600k - 300k - 10k = 290k, capped at 200k
        assert budget == 200_000

    def test_budget_capped_at_max_chunk(self, project, config):
        from engram.config import resolve_doc_paths
        paths = resolve_doc_paths(config, project)
        budget, _ = compute_budget(config, paths)
        assert budget <= config["budget"]["max_chunk_chars"]

    def test_negative_budget_clamped_to_zero(self, project, config):
        from engram.config import resolve_doc_paths
        paths = resolve_doc_paths(config, project)

        # Write living docs that exceed context_limit - overhead
        # context_limit=600k, overhead=10k → docs > 590k should force negative
        timeline = paths["timeline"]
        timeline.write_text("x" * 600_000)

        budget, living_chars = compute_budget(config, paths)
        assert budget == 0
        assert living_chars >= 600_000

    def test_index_headings_mode_uses_thin_budget_basis(self, project, config):
        from engram.config import resolve_doc_paths

        paths = resolve_doc_paths(config, project)
        config["budget"]["living_docs_budget_mode"] = "index_headings"

        timeline = paths["timeline"]
        body = ["# Timeline\n", "Updated by fold\n"] + ["x\n"] * 400_000
        timeline.write_text("".join(body))

        budget, living_chars = compute_budget(config, paths)
        full_chars, basis_chars = _living_docs_char_counts(paths, mode="index_headings")

        assert living_chars == full_chars
        assert full_chars > 500_000
        assert basis_chars < 5_000
        assert budget == config["budget"]["max_chunk_chars"]

    def test_context_pack_chars_reduce_budget(self, project, config):
        from engram.config import resolve_doc_paths

        paths = resolve_doc_paths(config, project)
        config["budget"]["living_docs_budget_mode"] = "index_headings"
        config["budget"]["context_limit_chars"] = 120_000
        config["budget"]["max_chunk_chars"] = 200_000

        budget_without_pack, _ = compute_budget(config, paths)
        budget_with_pack, _ = compute_budget(config, paths, context_pack_chars=50_000)

        assert budget_without_pack < 120_000
        assert budget_with_pack == max(0, budget_without_pack - 50_000)


class TestAdaptivePlanning:
    def test_predict_touched_ids_from_queue_text(self, project):
        doc_path = project / "docs" / "working" / "plan.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("Touches C012, E004 and W003 during revisits.")
        items = [_make_doc_item(path="docs/working/plan.md", chars=120)]

        predicted = _predict_touched_ids(items=items, project_root=project)
        assert predicted["C"] == ["C012"]
        assert predicted["E"] == ["E004"]
        assert predicted["W"] == ["W003"]

    def test_collect_context_pack_uses_existing_per_id_files(self, project, config):
        from engram.config import resolve_doc_paths

        paths = resolve_doc_paths(config, project)
        (paths["concepts"].with_suffix("") / "current").mkdir(parents=True, exist_ok=True)
        (paths["epistemic"].with_suffix("") / "current").mkdir(parents=True, exist_ok=True)
        (paths["workflows"].with_suffix("") / "current").mkdir(parents=True, exist_ok=True)

        (paths["concepts"].with_suffix("") / "current" / "C001.md").write_text("C" * 100)
        (paths["epistemic"].with_suffix("") / "current" / "E002.md").write_text("E" * 80)
        (paths["workflows"].with_suffix("") / "current" / "W003.md").write_text("W" * 60)

        files, chars, included = _collect_context_pack(
            doc_paths=paths,
            predicted_ids={"C": ["C001"], "E": ["E002"], "W": ["W003"]},
            max_ids_per_type=4,
            max_chars=500,
        )

        assert len(files) == 3
        assert chars == 240
        assert included == {"C": ["C001"], "E": ["E002"], "W": ["W003"]}


# ------------------------------------------------------------------
# Full scan_drift integration
# ------------------------------------------------------------------


class TestScanDrift:
    def test_no_drift_on_empty_docs(self, project, config):
        report = scan_drift(config, project)
        assert report.orphaned_concepts == []
        assert report.epistemic_audit == []
        assert report.contested_claims == []
        assert report.stale_unverified == []
        assert report.workflow_repetitions == []
        assert report.triggered(config["thresholds"]) is None

    def test_orphan_drift_triggered(self, project, config):
        registry = project / "docs" / "decisions" / "concept_registry.md"
        entries = "# Concept Registry\n\n"
        for i in range(4):
            entries += f"## C{i:03d}: concept_{i} (ACTIVE)\n- **Code:** `missing/{i}.py`\n\n"
        registry.write_text(entries)

        report = scan_drift(config, project)
        assert len(report.orphaned_concepts) == 4
        # threshold is 3, so 4 > 3 triggers
        assert report.triggered(config["thresholds"]) == "orphan_triage"

    def test_epistemic_audit_detected_and_triggered(self, project, config):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E008: harness phase 0 completion (believed)\n"
            "**History:**\n"
            f"- {old_date}: backlog noted\n"
        )

        config["thresholds"]["stale_epistemic_days"] = 90
        report = scan_drift(config, project)
        assert len(report.epistemic_audit) == 1
        assert report.epistemic_audit[0]["id"] == "E008"
        assert report.triggered(config["thresholds"]) == "epistemic_audit"


# ------------------------------------------------------------------
# Item rendering
# ------------------------------------------------------------------


class TestRenderItemContent:
    def test_doc_initial(self, project):
        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Spec\nSome content here.\n")

        item = _make_doc_item(path="docs/working/spec.md", date="2025-03-10T12:00:00")
        result = _render_item_content(item, project)
        assert "## [INITIAL] Doc: docs/working/spec.md" in result
        assert "2025-03-10" in result
        assert "Some content here." in result

    def test_doc_revisit(self, project):
        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Updated spec\n")

        item = {
            "date": "2025-06-01T00:00:00",
            "type": "doc",
            "path": "docs/working/spec.md",
            "chars": 100,
            "pass": "revisit",
            "first_seen_date": "2025-03-10T00:00:00",
        }
        result = _render_item_content(item, project)
        assert "[REVISIT]" in result
        assert "First seen:" in result
        assert "updated since first processed" in result

    def test_issue_item(self, project):
        issue_dir = project / "local_data" / "issues"
        issue_dir.mkdir(parents=True)
        issue_data = {
            "number": 42,
            "title": "Fix bug",
            "body": "Description here",
            "state": "open",
            "createdAt": "2025-04-01T00:00:00Z",
            "labels": [],
            "comments": [],
        }
        (issue_dir / "42.json").write_text(json.dumps(issue_data))

        item = {
            "date": "2025-04-01T00:00:00Z",
            "type": "issue",
            "path": "local_data/issues/42.json",
            "chars": 200,
            "pass": "initial",
            "issue_number": 42,
            "issue_title": "Fix bug",
        }
        result = _render_item_content(item, project)
        assert "Issue #42: Fix bug" in result

    def test_missing_file_graceful(self, project):
        item = _make_doc_item(path="nonexistent.md")
        result = _render_item_content(item, project)
        assert "FILE NOT FOUND" in result

    def test_prompts_item(self, project):
        session_dir = project / ".engram" / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "abc123.md").write_text("User: do the thing\n")

        item = {
            "date": "2025-05-01T00:00:00",
            "type": "prompts",
            "path": ".engram/sessions/abc123.md",
            "chars": 100,
            "pass": "initial",
            "session_id": "abc123",
            "prompt_count": 5,
        }
        result = _render_item_content(item, project)
        assert "[USER PROMPTS] Session (5 prompts)" in result
        assert "do the thing" in result

    def test_prompts_item_compacts_sm_noise(self, project):
        session_dir = project / ".engram" / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "abc123.md").write_text(
            "**[10:00]** [sm wait] worker idle\n\n"
            "**[10:01]** user decision text\n\n"
            "**[10:02]** [Input from: architect] " + ("x" * 500) + "\n\n"
            "**[10:03]** user decision text\n\n",
        )

        item = {
            "date": "2025-05-01T00:00:00",
            "type": "prompts",
            "path": ".engram/sessions/abc123.md",
            "chars": 100,
            "pass": "initial",
            "session_id": "abc123",
            "prompt_count": 4,
        }
        result = _render_item_content(item, project)
        assert "[sm wait]" not in result
        assert "user decision text" in result
        assert result.count("user decision text") == 2
        assert "[Input from: architect]" in result
        assert "..." in result


# ------------------------------------------------------------------
# next_chunk integration
# ------------------------------------------------------------------


class TestNextChunk:
    def test_normal_chunk(self, project, config):
        # Create a doc to reference from the queue
        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Test Spec\nContent for the chunk.\n")

        _write_queue(project, [
            _make_doc_item(path="docs/working/spec.md", chars=100),
        ])

        result = next_chunk(config, project)
        assert result.chunk_type == "fold"
        assert result.chunk_id == 1
        assert result.items_count == 1
        assert result.input_path.exists()
        assert result.prompt_path.exists()
        assert result.remaining_queue == 0
        assert result.context_worktree_path is None

        # Verify input file content
        input_text = result.input_path.read_text()
        assert "# Instructions" in input_text
        assert "Pre-assigned IDs for this chunk" in input_text
        assert "Every timeline phase (`## Phase: ...`) must include an `IDs:` line" in input_text
        assert "Epistemic Per-ID File Requirement (Required)" in input_text
        assert "Per-ID current files (" in input_text
        assert "should be detailed and coherent" in input_text
        assert "For normal fold chunks, use ONLY this input file + the 4 living docs." in input_text
        assert "Do NOT inspect source code, git history, or filesystem state to verify claims." in input_text
        assert "[Pasted text #N +M lines]" in input_text
        assert "Content for the chunk." in input_text

        # Verify prompt file content
        prompt_text = result.prompt_path.read_text()
        assert "knowledge fold chunk" in prompt_text
        assert "Pre-assigned IDs for this chunk" in prompt_text
        assert "Every timeline phase entry must include 'IDs:'" in prompt_text
        assert "For standard fold/workflow_synthesis chunks, use only the input file + living docs." in prompt_text
        assert "Do NOT inspect source code/git/filesystem for this chunk." in prompt_text
        assert "/epistemic_state/current/E*.md" in prompt_text
        assert "/epistemic_state/history/E*.md" in prompt_text
        assert "should be detailed and coherent, not terse" in prompt_text
        assert "/concept_registry/current/C*.md" in prompt_text
        assert "/workflow_registry/current/W*.md" in prompt_text

    def test_normal_chunk_reports_adaptive_planning_metadata(self, project, config):
        config["budget"]["living_docs_budget_mode"] = "index_headings"
        config["budget"]["planning_preview_items"] = 8
        config["budget"]["adaptive_context_max_ids_per_type"] = 4
        config["budget"]["adaptive_context_max_chars"] = 1000

        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("Touches C001 E001 W001 in one pass.\n")

        concepts_current = project / "docs" / "decisions" / "concept_registry" / "current"
        epistemic_current = project / "docs" / "decisions" / "epistemic_state" / "current"
        workflows_current = project / "docs" / "decisions" / "workflow_registry" / "current"
        concepts_current.mkdir(parents=True, exist_ok=True)
        epistemic_current.mkdir(parents=True, exist_ok=True)
        workflows_current.mkdir(parents=True, exist_ok=True)
        (concepts_current / "C001.md").write_text("concept detail\n")
        (epistemic_current / "E001.md").write_text("epistemic detail\n")
        (workflows_current / "W001.md").write_text("workflow detail\n")

        _write_queue(project, [_make_doc_item(path="docs/working/spec.md", chars=120)])
        result = next_chunk(config, project)

        assert result.planning_predicted_ids.get("C") == ["C001"]
        assert result.planning_predicted_ids.get("E") == ["E001"]
        assert result.planning_predicted_ids.get("W") == ["W001"]
        assert result.planning_context_ids == {"C": ["C001"], "E": ["E001"], "W": ["W001"]}
        assert result.planning_context_chars > 0
        assert isinstance(result.living_docs_budget_chars, int)

    def test_normal_chunk_creates_context_worktree_in_git_repo(self, project, config):
        subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
        subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)

        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Test Spec\nContext checkout.\n")
        _write_queue(project, [_make_doc_item(path="docs/working/spec.md", chars=100)])

        result = next_chunk(config, project)
        assert result.context_worktree_path is not None
        assert result.context_commit is not None
        assert result.context_worktree_path.exists()

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert result.context_commit == head

        prompt_text = result.prompt_path.read_text()
        assert str(result.context_worktree_path) in prompt_text
        assert "Do NOT inspect source code/git/filesystem for this chunk." in prompt_text
        assert "ignore it unless a future triage chunk explicitly requires repo verification." in prompt_text

        cleanup_chunk_context_worktree(project, result.context_worktree_path)
        assert not result.context_worktree_path.exists()

    def test_prompt_uses_split_epistemic_paths_when_legacy_files_present(self, project, config):
        legacy = project / "docs" / "decisions" / "epistemic_state" / "E005.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("## E005: legacy history\n")

        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Test Spec\nLegacy path prompt\n")
        _write_queue(project, [_make_doc_item(path="docs/working/spec.md", chars=100)])

        result = next_chunk(config, project)
        prompt_text = result.prompt_path.read_text()
        assert "/epistemic_state/current/E*.md" in prompt_text
        assert "/epistemic_state/history/E*.md" in prompt_text
        assert "/epistemic_state/E*.md" not in prompt_text

    def test_pre_assigned_ids_do_not_collide_with_existing_doc_ids(self, project, config):
        # Seed living docs with high existing IDs (simulates fresh allocator DB behind docs)
        (project / "docs" / "decisions" / "concept_registry.md").write_text(
            "# Concept Registry\n\n## C107: existing (ACTIVE)\n- **Code:** x\n",
        )
        (project / "docs" / "decisions" / "epistemic_state.md").write_text(
            "# Epistemic State\n\n## E101: existing (believed)\n**Current position:** x\n**Agent guidance:** x\n",
        )
        (project / "docs" / "decisions" / "workflow_registry.md").write_text(
            "# Workflow Registry\n\n## W001: existing (CURRENT)\n- **Context:** x\n- **Trigger:** x\n",
        )

        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Test Spec\nContent\n")

        item = _make_doc_item(path="docs/working/spec.md", chars=100, date="2026-01-06T00:00:00")
        item["entity_hints"] = [{"category": "C"}, {"category": "E"}, {"category": "W"}]
        _write_queue(project, [item])

        result = next_chunk(config, project)
        assert result.chunk_type == "fold"
        assert result.pre_assigned_ids["C"] == ["C108"]
        assert result.pre_assigned_ids["E"] == ["E102"]
        assert result.pre_assigned_ids["W"] == ["W002"]
        input_text = result.input_path.read_text()
        assert "/epistemic_state/current/E102.md" in input_text
        assert "/epistemic_state/history/E102.md" in input_text

    def test_workflow_preassign_novelty_gate_prefers_existing_variant(self, project, config):
        (project / "docs" / "decisions" / "workflow_registry.md").write_text(
            "# Workflow Registry\n\n"
            "## W001: dev_branch_workflow (CURRENT)\n- **Context:** baseline.\n\n"
            "## W005: epistemic_audit_evidence_gate (CURRENT)\n- **Context:** audit.\n\n"
            "## W013: subsystem_deletion_epic (CURRENT)\n- **Context:** deletion.\n",
        )

        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Test Spec\nGeneral updates\n")
        _write_queue(project, [_make_doc_item(path="docs/working/spec.md", chars=100)])

        result = next_chunk(config, project)
        assert result.chunk_type == "fold"
        assert "W" not in result.pre_assigned_ids
        input_text = result.input_path.read_text()
        assert "No workflow IDs were pre-assigned by novelty gate." in input_text

    def test_workflow_synthesis_cooldown_after_recent_new_workflow(self, project, config):
        workflows = project / "docs" / "decisions" / "workflow_registry.md"
        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n- **Context:** Deploy.\n\n"
            "## W005: audit_process (CURRENT)\n- **Context:** Audit.\n\n"
            "## W013: deletion_process (CURRENT)\n- **Context:** Delete.\n\n"
            "## W025: windowed_loop (CURRENT)\n- **Context:** Windowed loop.\n\n",
        )

        # Simulate immediately previous fold pre-assigning W025.
        chunks_dir = project / ".engram" / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        (chunks_dir / "chunk_001_input.md").write_text("# prior\n")
        manifest = project / ".engram" / "chunks_manifest.yaml"
        manifest.write_text(
            "- id: 1\n"
            "  date_range: \"2026-01-14 to 2026-01-14\"\n"
            "  items: 1\n"
            "  chars: 100\n"
            "  input_file: chunk_001_input.md\n"
            "  pre_assigned_workflow_ids:\n"
            "    - W025\n",
        )

        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("Content")
        _write_queue(project, [_make_doc_item(path="docs/working/spec.md", chars=100)])

        result = next_chunk(config, project)
        assert result.chunk_type == "fold"

    def test_fold_chunk_does_not_include_orphan_triage_section(self, project, config):
        registry = project / "docs" / "decisions" / "concept_registry.md"
        registry.write_text(
            "# Concept Registry\n\n"
            "## C001: orphaned_concept (ACTIVE)\n"
            "- **Code:** missing/thing.py\n"
        )
        config["thresholds"]["orphan_triage"] = 50  # ensure drift doesn't trigger triage

        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("Content")
        _write_queue(project, [_make_doc_item(chars=100)])

        result = next_chunk(config, project)
        assert result.chunk_type == "fold"
        input_text = result.input_path.read_text()
        assert "[ORPHANED CONCEPTS]" not in input_text

    def test_drift_triage_chunk(self, project, config):
        # Create orphaned concepts exceeding threshold (3)
        registry = project / "docs" / "decisions" / "concept_registry.md"
        entries = "# Concept Registry\n\n"
        for i in range(4):
            entries += f"## C{i:03d}: orphan_{i} (ACTIVE)\n- **Code:** `gone/{i}.py`\n\n"
        registry.write_text(entries)

        _write_queue(project, [
            _make_doc_item(chars=100),
        ])

        result = next_chunk(config, project)
        assert result.chunk_type == "orphan_triage"
        assert result.items_count == 0
        assert result.drift_entry_count == 4
        assert result.remaining_queue == 1  # queue not consumed

        input_text = result.input_path.read_text()
        assert "Orphan Triage Round" in input_text
        assert "orphan_0" in input_text
        prompt_text = result.prompt_path.read_text()
        assert "Follow triage input instructions for the correct repo view" in prompt_text

    def test_epistemic_audit_chunk(self, project, config):
        epistemic = project / "docs" / "decisions" / "epistemic_state.md"
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
        epistemic.write_text(
            "# Epistemic State\n\n"
            "## E008: harness phase 0 completion (believed)\n"
            "**History:**\n"
            f"- {old_date}: backlog noted\n"
        )
        config["thresholds"]["stale_epistemic_days"] = 90

        _write_queue(project, [
            _make_doc_item(chars=100),
        ])

        result = next_chunk(config, project)
        assert result.chunk_type == "epistemic_audit"
        assert result.items_count == 0
        assert result.drift_entry_count == 1
        assert result.remaining_queue == 1

        input_text = result.input_path.read_text()
        assert "Epistemic Audit Round" in input_text
        assert "harness phase 0 completion" in input_text

    def test_workflow_synthesis_not_repeated_for_same_ids(self, project, config):
        """workflow_synthesis should not immediately re-fire when workflows are unchanged."""
        workflows = project / "docs" / "decisions" / "workflow_registry.md"
        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n- **Context:** Deploy.\n\n"
            "## W002: review_process (CURRENT)\n- **Context:** Review.\n\n"
            "## W003: test_process (CURRENT)\n- **Context:** Test.\n\n"
            "## W004: release_process (CURRENT)\n- **Context:** Release.\n\n"
        )
        _write_queue(project, [_make_doc_item(chars=100), _make_doc_item(chars=100)])

        # First call: synthesis fires for W001-W004
        r1 = next_chunk(config, project)
        assert r1.chunk_type == "workflow_synthesis"
        assert r1.drift_entry_count == 4
        workflow_text = r1.input_path.read_text()
        assert "W001" in workflow_text
        assert ")\n- **W002" in workflow_text
        assert "Input-only mode for this chunk:" in workflow_text
        assert "Do NOT inspect source code, git history, or filesystem state." in workflow_text

        # Second call: unchanged registry triggers cooldown suppression, so we proceed with a normal fold chunk.
        r2 = next_chunk(config, project)
        assert r2.chunk_type == "fold", (
            f"Expected fold chunk after synthesis already ran, got {r2.chunk_type}"
        )

    def test_workflow_synthesis_not_repeated_for_same_ids_after_edit(self, project, config):
        """workflow_synthesis should cooldown when only same CURRENT IDs are edited."""
        workflows = project / "docs" / "decisions" / "workflow_registry.md"
        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n- **Context:** Deploy.\n\n"
            "## W002: review_process (CURRENT)\n- **Context:** Review.\n\n"
            "## W003: test_process (CURRENT)\n- **Context:** Test.\n\n"
            "## W004: release_process (CURRENT)\n- **Context:** Release.\n\n"
        )
        _write_queue(project, [_make_doc_item(chars=100), _make_doc_item(chars=100)])

        first = next_chunk(config, project)
        assert first.chunk_type == "workflow_synthesis"

        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n"
            "- **Context:** Deploy.\n"
            "- **Current method:** Clarified without changing workflow membership.\n\n"
            "## W002: review_process (CURRENT)\n- **Context:** Review.\n\n"
            "## W003: test_process (CURRENT)\n- **Context:** Test.\n\n"
            "## W004: release_process (CURRENT)\n- **Context:** Release.\n\n"
        )

        second = next_chunk(config, project)
        assert second.chunk_type == "fold", (
            f"Expected fold chunk after same-ID edit, got {second.chunk_type}"
        )

    def test_workflow_synthesis_can_retry_after_cooldown(self, project, config):
        workflows = project / "docs" / "decisions" / "workflow_registry.md"
        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n- **Context:** Deploy.\n\n"
            "## W002: review_process (CURRENT)\n- **Context:** Review.\n\n"
            "## W003: test_process (CURRENT)\n- **Context:** Test.\n\n"
            "## W004: release_process (CURRENT)\n- **Context:** Release.\n\n"
        )

        config["budget"]["max_chunk_chars"] = 60
        items = [_make_doc_item(chars=60, path=f"docs/working/{i}.md") for i in range(10)]
        for item in items:
            p = project / item["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        _write_queue(project, items)

        first = next_chunk(config, project)
        assert first.chunk_type == "workflow_synthesis"

        # Produce 5 fold chunks during cooldown.
        for _ in range(5):
            nxt = next_chunk(config, project)
            assert nxt.chunk_type == "fold"

        # Cooldown expired, unchanged registry can re-trigger synthesis.
        retried = next_chunk(config, project)
        assert retried.chunk_type == "workflow_synthesis"

    def test_workflow_synthesis_fires_when_new_workflows_added(self, project, config):
        """workflow_synthesis must re-fire when workflow_registry changes after a prior attempt."""
        workflows = project / "docs" / "decisions" / "workflow_registry.md"
        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n- **Context:** Deploy.\n\n"
            "## W002: review_process (CURRENT)\n- **Context:** Review.\n\n"
            "## W003: test_process (CURRENT)\n- **Context:** Test.\n\n"
            "## W004: release_process (CURRENT)\n- **Context:** Release.\n\n"
        )
        _write_queue(project, [_make_doc_item(chars=100)])

        # First synthesis covers W001-W004
        r1 = next_chunk(config, project)
        assert r1.chunk_type == "workflow_synthesis"

        # New workflows added — W005, W006, W007 are unseen
        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n- **Context:** Deploy.\n\n"
            "## W002: review_process (CURRENT)\n- **Context:** Review.\n\n"
            "## W003: test_process (CURRENT)\n- **Context:** Test.\n\n"
            "## W004: release_process (CURRENT)\n- **Context:** Release.\n\n"
            "## W005: monitor_process (CURRENT)\n- **Context:** Monitor.\n\n"
            "## W006: rollback_process (CURRENT)\n- **Context:** Rollback.\n\n"
            "## W007: audit_process (CURRENT)\n- **Context:** Audit.\n\n"
        )

        # Second call: W005-W007 are new → synthesis fires with full set (7 entries)
        r2 = next_chunk(config, project)
        assert r2.chunk_type == "workflow_synthesis", (
            f"Expected synthesis to re-fire for new workflows, got {r2.chunk_type}"
        )
        assert r2.drift_entry_count == 7

    def test_workflow_synthesis_malformed_manifest_degrades_gracefully(self, project, config):
        """A malformed manifest must not abort next_chunk — synthesis fires as if no prior attempt."""
        workflows = project / "docs" / "decisions" / "workflow_registry.md"
        workflows.write_text(
            "# Workflow Registry\n\n"
            "## W001: deploy_process (CURRENT)\n- **Context:** Deploy.\n\n"
            "## W002: review_process (CURRENT)\n- **Context:** Review.\n\n"
            "## W003: test_process (CURRENT)\n- **Context:** Test.\n\n"
            "## W004: release_process (CURRENT)\n- **Context:** Release.\n\n"
        )
        # Write a malformed manifest
        manifest = project / ".engram" / "chunks_manifest.yaml"
        manifest.write_text("- id: 1\n  type: workflow_synthesis\n  workflow_registry_hash: [\n  bad yaml here\n")

        _write_queue(project, [_make_doc_item(chars=100)])

        # Must not raise — synthesis fires normally (dedup skipped)
        result = next_chunk(config, project)
        assert result.chunk_type == "workflow_synthesis"
        assert result.drift_entry_count == 4

    def test_queue_not_found_raises(self, project, config):
        with pytest.raises(FileNotFoundError, match="No queue found"):
            next_chunk(config, project)

    def test_empty_queue_raises(self, project, config):
        _write_queue(project, [])
        with pytest.raises(ValueError, match="Queue is empty"):
            next_chunk(config, project)

    def test_multiple_chunks_increment_id(self, project, config):
        doc_path = project / "docs" / "working" / "a.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("A")
        doc_b = project / "docs" / "working" / "b.md"
        doc_b.write_text("B")

        # Use chars large enough that both won't fit in max_chunk_chars
        config["budget"]["max_chunk_chars"] = 150

        _write_queue(project, [
            _make_doc_item(path="docs/working/a.md", chars=100, date="2025-01-01T00:00:00"),
            _make_doc_item(path="docs/working/b.md", chars=100, date="2025-02-01T00:00:00"),
        ])

        r1 = next_chunk(config, project)
        assert r1.chunk_id == 1
        assert r1.remaining_queue == 1

        r2 = next_chunk(config, project)
        assert r2.chunk_id == 2
        assert r2.remaining_queue == 0

    def test_oversized_single_item(self, project, config):
        doc_path = project / "docs" / "working" / "huge.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("x" * 300_000)

        _write_queue(project, [
            _make_doc_item(path="docs/working/huge.md", chars=300_000),
        ])

        result = next_chunk(config, project)
        assert result.chunk_type == "fold"
        assert result.items_count == 1

    def test_budget_limits_items(self, project, config):
        # Create multiple doc files
        (project / "docs" / "working").mkdir(parents=True, exist_ok=True)
        items = []
        for i in range(10):
            p = f"docs/working/doc_{i}.md"
            (project / p).write_text(f"Content {i}\n")
            items.append(_make_doc_item(
                path=p,
                chars=50_000,
                date=f"2025-01-{i+1:02d}T00:00:00",
            ))

        _write_queue(project, items)

        result = next_chunk(config, project)
        # Budget is ~200k, each item is 50k chars, so should fit 4 items
        assert result.items_count == 4
        assert result.remaining_queue == 6

    def test_manifest_written(self, project, config):
        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("Spec")

        _write_queue(project, [_make_doc_item(path="docs/working/spec.md", chars=100)])

        next_chunk(config, project)
        manifest = project / ".engram" / "chunks_manifest.yaml"
        assert manifest.exists()
        text = manifest.read_text()
        assert "id: 1" in text
        assert "input_file: chunk_001_input.md" in text

    def test_id_pre_assignment(self, project, config):
        doc_path = project / "docs" / "working" / "spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("Spec")

        # Queue item with entity hints
        item = _make_doc_item(path="docs/working/spec.md", chars=100)
        item["entity_hints"] = [
            {"category": "C"},
            {"category": "C"},
            {"category": "E"},
        ]
        _write_queue(project, [item])

        result = next_chunk(config, project)
        assert "C" in result.pre_assigned_ids
        assert len(result.pre_assigned_ids["C"]) == 2
        assert "E" in result.pre_assigned_ids
        assert len(result.pre_assigned_ids["E"]) == 1
        # IDs should be formatted correctly
        assert result.pre_assigned_ids["C"][0].startswith("C")

        # Verify IDs appear in input file
        input_text = result.input_path.read_text()
        for cid in result.pre_assigned_ids["C"]:
            assert cid in input_text

    def test_manifest_records_pre_assigned_workflow_ids(self, project, config):
        doc_path = project / "docs" / "working" / "workflow_spec.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("workflow changes")

        item = _make_doc_item(path="docs/working/workflow_spec.md", chars=100)
        item["entity_hints"] = [{"category": "W"}]
        _write_queue(project, [item])

        result = next_chunk(config, project)
        assert result.chunk_type == "fold"
        assert result.pre_assigned_ids["W"] == ["W001"]

        manifest_text = (project / ".engram" / "chunks_manifest.yaml").read_text()
        assert "pre_assigned_workflow_ids" in manifest_text
        assert "- W001" in manifest_text
