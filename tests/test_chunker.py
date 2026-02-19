"""Tests for chunk assembly and drift-priority scheduling."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engram.fold.chunker import (
    ChunkResult,
    DriftReport,
    _extract_code_paths,
    _extract_latest_date,
    _find_claims_by_status,
    _find_orphaned_concepts,
    _find_workflow_repetitions,
    _render_item_content,
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


# ------------------------------------------------------------------
# Full scan_drift integration
# ------------------------------------------------------------------


class TestScanDrift:
    def test_no_drift_on_empty_docs(self, project, config):
        report = scan_drift(config, project)
        assert report.orphaned_concepts == []
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

        # Verify input file content
        input_text = result.input_path.read_text()
        assert "# Instructions" in input_text
        assert "Content for the chunk." in input_text

        # Verify prompt file content
        prompt_text = result.prompt_path.read_text()
        assert "knowledge fold chunk" in prompt_text

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

    def test_workflow_synthesis_not_repeated_for_same_ids(self, project, config):
        """workflow_synthesis should not re-fire when all CURRENT IDs were already synthesized."""
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

        # Agent kept all workflows CURRENT — second call must NOT re-fire synthesis.
        r2 = next_chunk(config, project)
        assert r2.chunk_type == "fold", (
            f"Expected fold chunk after synthesis already ran, got {r2.chunk_type}"
        )

    def test_workflow_synthesis_fires_when_new_workflows_added(self, project, config):
        """workflow_synthesis must re-fire when new workflow IDs appear after a prior synthesis."""
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
