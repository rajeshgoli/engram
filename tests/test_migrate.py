"""Tests for engram.migrate — v2 → v3 migration.

Tests: idempotency, ID assignment, 4th doc extraction, graveyard moves,
cross-ref resolution, counter initialization, fold continuation marker.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
import yaml

from engram.cli import GRAVEYARD_HEADERS, LIVING_DOC_HEADERS
from engram.compact.graveyard import compact_living_doc
from engram.migrate import (
    backfill_ids,
    extract_workflows,
    initialize_counters,
    migrate,
    rewrite_cross_references,
    set_fold_marker,
)
from engram.parse import extract_id, is_stub, parse_sections


# ---------------------------------------------------------------------------
# Fixtures: synthetic v2 docs
# ---------------------------------------------------------------------------

V2_CONCEPT_REGISTRY = """\
# Concept Registry

Code concepts with liveness status.

## proximity_pruning (DEAD)
- **Code:** `src/pruning.py`
- **Issues:** #100, #200
- Replaced by structure-driven pruning.

## check_level_hits (ACTIVE)
- **Code:** `SignalDetector.check_level_hits()`, `level_tracking.py`
- **Issues:** #1302, #1418
- Key entry point for level monitoring.

## fractal_detector (EVOLVED)
- **Code:** `src/detect.py`
- Evolved into multi-scale detector.
"""

V2_EPISTEMIC_STATE = """\
# Epistemic State

Claims and beliefs about the system.

## 74_percent_win_rate (refuted)
- **Evidence:** #1343 claimed 74% WR. Refuted by #1404 (same-bar bug).
- Corrected to 48.9%.

## tick_level_edge (unverified)
- **Evidence:** #1418 hypothesizes 69.7% at tick level.
- Needs broker fill data to confirm.

## momentum_dominates (believed)
- **Evidence:** Multiple backtests confirm momentum > mean reversion.
- **History:** Initially contested, resolved by #1500.
"""

V2_CONCEPT_WITH_WORKFLOWS = """\
# Concept Registry

Code concepts with liveness status.

## investigation_protocol (ACTIVE)
- **Context:** How agents investigate bugs
- **Current method:** Deterministic debugging
- **Trigger for change:** #1404

## check_level_hits (ACTIVE)
- **Code:** `level_tracking.py`
- **Issues:** #1302
"""

V2_WORKFLOW_REGISTRY_EMPTY = """\
# Workflow Registry

Process patterns keyed by stable ID (W###).
Status: CURRENT / SUPERSEDED / MERGED.
"""

V2_WORKFLOW_REGISTRY_WITH_ENTRIES = """\
# Workflow Registry

Process patterns keyed by stable ID (W###).
Status: CURRENT / SUPERSEDED / MERGED.

## agent_handoff (CURRENT)
- **Context:** How work transfers between sessions
- **Current method:** sm send with structured messages
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_project(tmp_path: Path, concept_content: str = V2_CONCEPT_REGISTRY,
                    epistemic_content: str = V2_EPISTEMIC_STATE,
                    workflow_content: str = V2_WORKFLOW_REGISTRY_EMPTY,
                    timeline_content: str | None = None) -> Path:
    """Create a minimal project structure with .engram/ config and v2 docs."""
    project = tmp_path / "project"
    project.mkdir()

    # Create .engram/config.yaml
    engram_dir = project / ".engram"
    engram_dir.mkdir()
    config = {
        "living_docs": {
            "timeline": "docs/timeline.md",
            "concepts": "docs/concept_registry.md",
            "epistemic": "docs/epistemic_state.md",
            "workflows": "docs/workflow_registry.md",
        },
        "graveyard": {
            "concepts": "docs/concept_graveyard.md",
            "epistemic": "docs/epistemic_graveyard.md",
        },
    }
    (engram_dir / "config.yaml").write_text(yaml.dump(config))

    # Create doc files
    docs_dir = project / "docs"
    docs_dir.mkdir()
    (docs_dir / "concept_registry.md").write_text(concept_content)
    (docs_dir / "epistemic_state.md").write_text(epistemic_content)
    (docs_dir / "workflow_registry.md").write_text(workflow_content)

    tl = timeline_content or LIVING_DOC_HEADERS["timeline"]
    (docs_dir / "timeline.md").write_text(tl)

    return project


# ---------------------------------------------------------------------------
# Phase 1: ID backfill
# ---------------------------------------------------------------------------

class TestBackfillIds:

    def test_assigns_ids_in_document_order(self):
        counters = {"C": 1, "E": 1, "W": 1}
        result, name_map, counters = backfill_ids(
            V2_CONCEPT_REGISTRY, "concepts", counters,
        )
        sections = parse_sections(result)

        assert extract_id(sections[0]["heading"]) == "C001"
        assert extract_id(sections[1]["heading"]) == "C002"
        assert extract_id(sections[2]["heading"]) == "C003"

    def test_name_to_id_mapping(self):
        counters = {"C": 1, "E": 1, "W": 1}
        _, name_map, _ = backfill_ids(
            V2_CONCEPT_REGISTRY, "concepts", counters,
        )
        assert name_map["proximity_pruning"] == "C001"
        assert name_map["check_level_hits"] == "C002"
        assert name_map["fractal_detector"] == "C003"

    def test_counter_advances(self):
        counters = {"C": 1, "E": 1, "W": 1}
        _, _, counters = backfill_ids(
            V2_CONCEPT_REGISTRY, "concepts", counters,
        )
        assert counters["C"] == 4  # 3 entries assigned

    def test_preserves_body_content(self):
        counters = {"C": 1, "E": 1, "W": 1}
        result, _, _ = backfill_ids(
            V2_CONCEPT_REGISTRY, "concepts", counters,
        )
        assert "`SignalDetector.check_level_hits()`" in result
        assert "#1302" in result

    def test_normalizes_status(self):
        counters = {"C": 1, "E": 1, "W": 1}
        result, _, _ = backfill_ids(
            V2_CONCEPT_REGISTRY, "concepts", counters,
        )
        assert "(DEAD)" in result
        assert "(ACTIVE)" in result
        assert "(EVOLVED)" in result

    def test_epistemic_ids(self):
        counters = {"C": 1, "E": 1, "W": 1}
        result, name_map, counters = backfill_ids(
            V2_EPISTEMIC_STATE, "epistemic", counters,
        )
        sections = parse_sections(result)
        assert extract_id(sections[0]["heading"]) == "E001"
        assert extract_id(sections[1]["heading"]) == "E002"
        assert extract_id(sections[2]["heading"]) == "E003"
        assert counters["E"] == 4

    def test_skips_already_id_entries(self):
        """Entries that already have IDs are preserved as-is."""
        content = """\
# Concept Registry

## C001: proximity_pruning (DEAD)
- **Code:** `src/pruning.py`

## new_concept (ACTIVE)
- **Code:** `src/new.py`
"""
        counters = {"C": 2, "E": 1, "W": 1}  # Start at 2 since C001 exists
        result, name_map, counters = backfill_ids(content, "concepts", counters)
        sections = parse_sections(result)

        assert extract_id(sections[0]["heading"]) == "C001"
        assert extract_id(sections[1]["heading"]) == "C002"
        assert counters["C"] == 3

    def test_preserves_preamble(self):
        counters = {"C": 1, "E": 1, "W": 1}
        result, _, _ = backfill_ids(
            V2_CONCEPT_REGISTRY, "concepts", counters,
        )
        assert result.startswith("# Concept Registry")

    def test_counter_starts_from_provided_value(self):
        counters = {"C": 10, "E": 1, "W": 1}
        result, name_map, counters = backfill_ids(
            V2_CONCEPT_REGISTRY, "concepts", counters,
        )
        assert name_map["proximity_pruning"] == "C010"
        assert counters["C"] == 13


# ---------------------------------------------------------------------------
# Phase 2: Workflow extraction
# ---------------------------------------------------------------------------

class TestExtractWorkflows:

    def test_moves_workflow_entries_to_workflow_doc(self):
        counters = {"C": 1, "E": 1, "W": 1}
        # First backfill IDs on concept doc
        concept_with_ids, _, counters = backfill_ids(
            V2_CONCEPT_WITH_WORKFLOWS, "concepts", counters,
        )

        new_c, new_e, new_w, wf_map, counters = extract_workflows(
            concept_with_ids,
            V2_EPISTEMIC_STATE,
            V2_WORKFLOW_REGISTRY_EMPTY,
            counters,
        )

        # Workflow entry should be removed from concepts
        concept_sections = parse_sections(new_c)
        concept_names = [s["heading"] for s in concept_sections]
        assert not any("investigation_protocol" in h for h in concept_names)

        # Workflow entry should appear in workflow doc
        assert "investigation_protocol" in new_w
        assert "Context:" in new_w

    def test_keeps_non_workflow_entries_in_place(self):
        counters = {"C": 1, "E": 1, "W": 1}
        concept_with_ids, _, counters = backfill_ids(
            V2_CONCEPT_WITH_WORKFLOWS, "concepts", counters,
        )

        new_c, _, _, _, _ = extract_workflows(
            concept_with_ids,
            V2_EPISTEMIC_STATE,
            V2_WORKFLOW_REGISTRY_EMPTY,
            counters,
        )

        assert "check_level_hits" in new_c
        assert "level_tracking.py" in new_c

    def test_assigns_w_ids_to_extracted_entries(self):
        counters = {"C": 1, "E": 1, "W": 1}
        concept_with_ids, _, counters = backfill_ids(
            V2_CONCEPT_WITH_WORKFLOWS, "concepts", counters,
        )

        _, _, new_w, wf_map, counters = extract_workflows(
            concept_with_ids,
            V2_EPISTEMIC_STATE,
            V2_WORKFLOW_REGISTRY_EMPTY,
            counters,
        )

        assert "investigation_protocol" in wf_map
        wf_id = wf_map["investigation_protocol"]
        assert wf_id.startswith("W")
        assert wf_id in new_w


# ---------------------------------------------------------------------------
# Phase 3: Graveyard bootstrapping
# ---------------------------------------------------------------------------

class TestGraveyardBootstrapping:
    """Tests that compact_living_doc (from compact/graveyard.py) works
    correctly in the migration context."""

    def test_moves_dead_to_graveyard(self, tmp_path):
        gy_path = tmp_path / "concept_graveyard.md"
        gy_path.write_text(GRAVEYARD_HEADERS["concepts"])

        content = """\
# Concept Registry

## C001: proximity_pruning (DEAD)
- **Code:** `src/pruning.py`
- Replaced by structure-driven pruning.

## C002: check_level_hits (ACTIVE)
- **Code:** `level_tracking.py`
"""
        result, _ = compact_living_doc(content, "concepts", gy_path)

        # Living doc should have stub
        sections = parse_sections(result)
        dead_sec = [s for s in sections if "proximity_pruning" in s["heading"]]
        assert len(dead_sec) == 1
        assert is_stub(dead_sec[0]["heading"])
        assert "\u2192" in dead_sec[0]["heading"]

        # Graveyard should have full entry
        gy_content = gy_path.read_text()
        assert "proximity_pruning" in gy_content
        assert "src/pruning.py" in gy_content

    def test_keeps_active_in_place(self, tmp_path):
        gy_path = tmp_path / "concept_graveyard.md"
        gy_path.write_text(GRAVEYARD_HEADERS["concepts"])

        content = """\
# Concept Registry

## C001: check_level_hits (ACTIVE)
- **Code:** `level_tracking.py`
"""
        result, _ = compact_living_doc(content, "concepts", gy_path)

        sections = parse_sections(result)
        assert len(sections) == 1
        assert not is_stub(sections[0]["heading"])

    def test_moves_refuted_epistemic(self, tmp_path):
        gy_path = tmp_path / "epistemic_graveyard.md"
        gy_path.write_text(GRAVEYARD_HEADERS["epistemic"])

        content = """\
# Epistemic State

## E001: 74_percent_win_rate (refuted)
- **Evidence:** Refuted by #1404.
- Corrected to 48.9%.

## E002: tick_level_edge (unverified)
- **Evidence:** Needs broker data.
"""
        result, _ = compact_living_doc(content, "epistemic", gy_path)

        sections = parse_sections(result)
        refuted = [s for s in sections if "74_percent" in s["heading"]]
        assert len(refuted) == 1
        assert is_stub(refuted[0]["heading"])

        unverified = [s for s in sections if "tick_level" in s["heading"]]
        assert len(unverified) == 1
        assert not is_stub(unverified[0]["heading"])


# ---------------------------------------------------------------------------
# Phase 4: Cross-reference rewrite
# ---------------------------------------------------------------------------

class TestRewriteCrossReferences:

    def test_replaces_see_name_with_id(self):
        name_map = {"proximity_pruning": "C001"}
        content = "Some text that says see proximity_pruning for details."
        result = rewrite_cross_references(content, name_map)
        assert "see C001" in result

    def test_replaces_supersedes_reference(self):
        name_map = {"old_method": "W002"}
        content = "- **Supersedes:** old_method"
        result = rewrite_cross_references(content, name_map)
        assert "W002" in result

    def test_replaces_related_concepts_reference(self):
        name_map = {"check_level_hits": "C042"}
        content = "**Related concepts:** check_level_hits, other_thing"
        result = rewrite_cross_references(content, name_map)
        assert "C042" in result

    def test_no_change_when_empty_map(self):
        content = "Some text with no references."
        result = rewrite_cross_references(content, {})
        assert result == content


# ---------------------------------------------------------------------------
# Phase 5: Counter initialization
# ---------------------------------------------------------------------------

class TestInitializeCounters:

    def test_sets_counters_from_max_ids(self, tmp_path):
        db_path = tmp_path / "engram.db"
        contents = {
            "concepts": "## C005: foo (ACTIVE)\n- **Code:** x\n\n## C010: bar (ACTIVE)\n- **Code:** y\n",
            "epistemic": "## E003: claim (believed)\n- **Evidence:** z\n",
            "workflows": "## W001: wf (CURRENT)\n- **Context:** a\n- **Trigger:** b\n",
        }
        result = initialize_counters(db_path, contents)
        assert result["C"] == 11  # max C010 + 1
        assert result["E"] == 4   # max E003 + 1
        assert result["W"] == 2   # max W001 + 1

    def test_counters_persisted_in_sqlite(self, tmp_path):
        db_path = tmp_path / "engram.db"
        contents = {
            "concepts": "## C003: foo (ACTIVE)\n- **Code:** x\n",
        }
        initialize_counters(db_path, contents)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT next_id FROM id_counters WHERE category = 'C'"
        ).fetchone()
        conn.close()
        assert row[0] == 4


# ---------------------------------------------------------------------------
# Phase 6: Fold continuation marker
# ---------------------------------------------------------------------------

class TestFoldMarker:

    def test_sets_marker_in_sqlite(self, tmp_path):
        db_path = tmp_path / "engram.db"
        set_fold_marker(db_path, date(2026, 1, 1))

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM server_state WHERE key = 'fold_from'"
        ).fetchone()
        conn.close()
        assert row[0] == "2026-01-01"

    def test_updates_existing_marker(self, tmp_path):
        db_path = tmp_path / "engram.db"
        set_fold_marker(db_path, date(2026, 1, 1))
        set_fold_marker(db_path, date(2026, 2, 15))

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM server_state WHERE key = 'fold_from'"
        ).fetchone()
        conn.close()
        assert row[0] == "2026-02-15"


# ---------------------------------------------------------------------------
# Full migration (integration)
# ---------------------------------------------------------------------------

class TestMigrate:

    def test_assigns_ids_to_all_entries(self, tmp_path):
        project = _create_project(tmp_path)
        lint_result, counters = migrate(project)

        concepts = (project / "docs" / "concept_registry.md").read_text()
        epistemic = (project / "docs" / "epistemic_state.md").read_text()

        for sec in parse_sections(concepts):
            assert extract_id(sec["heading"]) is not None

        for sec in parse_sections(epistemic):
            assert extract_id(sec["heading"]) is not None

    def test_creates_graveyard_files(self, tmp_path):
        project = _create_project(tmp_path)
        migrate(project)

        concept_gy = project / "docs" / "concept_graveyard.md"
        epistemic_gy = project / "docs" / "epistemic_graveyard.md"

        assert concept_gy.exists()
        assert epistemic_gy.exists()

        # DEAD entry should be in concept graveyard
        gy_content = concept_gy.read_text()
        assert "proximity_pruning" in gy_content

        # Refuted entry should be in epistemic graveyard
        gy_content = epistemic_gy.read_text()
        assert "74_percent_win_rate" in gy_content

    def test_dead_entries_become_stubs(self, tmp_path):
        project = _create_project(tmp_path)
        migrate(project)

        concepts = (project / "docs" / "concept_registry.md").read_text()
        sections = parse_sections(concepts)

        dead = [s for s in sections if "proximity_pruning" in s["heading"]]
        assert len(dead) == 1
        assert is_stub(dead[0]["heading"])

    def test_active_entries_preserved(self, tmp_path):
        project = _create_project(tmp_path)
        migrate(project)

        concepts = (project / "docs" / "concept_registry.md").read_text()
        sections = parse_sections(concepts)

        active = [s for s in sections if "check_level_hits" in s["heading"]]
        assert len(active) == 1
        assert not is_stub(active[0]["heading"])
        assert "Code:" in active[0]["text"]

    def test_counters_initialized(self, tmp_path):
        project = _create_project(tmp_path)
        _, counters = migrate(project)

        assert counters["C"] >= 1
        assert counters["E"] >= 1

        # Verify in SQLite
        db_path = project / ".engram" / "engram.db"
        conn = sqlite3.connect(str(db_path))
        rows = dict(conn.execute("SELECT category, next_id FROM id_counters").fetchall())
        conn.close()

        assert rows["C"] == counters["C"]
        assert rows["E"] == counters["E"]

    def test_fold_from_marker_set(self, tmp_path):
        project = _create_project(tmp_path)
        migrate(project, fold_from=date(2026, 1, 1))

        db_path = project / ".engram" / "engram.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM server_state WHERE key = 'fold_from'"
        ).fetchone()
        conn.close()
        assert row[0] == "2026-01-01"

    def test_idempotent(self, tmp_path):
        """Running migration twice produces the same result."""
        project = _create_project(tmp_path)

        # First run
        migrate(project)
        concepts_1 = (project / "docs" / "concept_registry.md").read_text()
        epistemic_1 = (project / "docs" / "epistemic_state.md").read_text()
        workflows_1 = (project / "docs" / "workflow_registry.md").read_text()
        gy_concepts_1 = (project / "docs" / "concept_graveyard.md").read_text()
        gy_epistemic_1 = (project / "docs" / "epistemic_graveyard.md").read_text()

        # Second run
        migrate(project)
        concepts_2 = (project / "docs" / "concept_registry.md").read_text()
        epistemic_2 = (project / "docs" / "epistemic_state.md").read_text()
        workflows_2 = (project / "docs" / "workflow_registry.md").read_text()
        gy_concepts_2 = (project / "docs" / "concept_graveyard.md").read_text()
        gy_epistemic_2 = (project / "docs" / "epistemic_graveyard.md").read_text()

        assert concepts_1 == concepts_2
        assert epistemic_1 == epistemic_2
        assert workflows_1 == workflows_2
        assert gy_concepts_1 == gy_concepts_2
        assert gy_epistemic_1 == gy_epistemic_2

    def test_idempotent_counters(self, tmp_path):
        """Counter values are the same after two runs."""
        project = _create_project(tmp_path)

        _, counters_1 = migrate(project)
        _, counters_2 = migrate(project)

        assert counters_1 == counters_2

    def test_workflow_extraction_in_full_migration(self, tmp_path):
        """Workflow-like entries in concept doc are extracted."""
        project = _create_project(
            tmp_path,
            concept_content=V2_CONCEPT_WITH_WORKFLOWS,
        )
        migrate(project)

        concepts = (project / "docs" / "concept_registry.md").read_text()
        workflows = (project / "docs" / "workflow_registry.md").read_text()

        # investigation_protocol should NOT be in concepts
        assert "investigation_protocol" not in concepts

        # investigation_protocol should be in workflows
        assert "investigation_protocol" in workflows
        assert "Context:" in workflows

    def test_lint_passes_after_migration(self, tmp_path):
        """Linter passes on migrated docs."""
        project = _create_project(tmp_path)
        lint_result, _ = migrate(project)

        assert lint_result.passed, (
            f"Lint failed: {[str(v) for v in lint_result.violations]}"
        )

    def test_existing_workflow_entries_get_ids(self, tmp_path):
        """Workflow entries already in workflow doc get IDs."""
        project = _create_project(
            tmp_path,
            workflow_content=V2_WORKFLOW_REGISTRY_WITH_ENTRIES,
        )
        migrate(project)

        workflows = (project / "docs" / "workflow_registry.md").read_text()
        sections = parse_sections(workflows)

        for sec in sections:
            assert extract_id(sec["heading"]) is not None

    def test_no_fold_from_means_no_marker(self, tmp_path):
        """Without --fold-from, no marker is set."""
        project = _create_project(tmp_path)
        migrate(project)

        db_path = project / ".engram" / "engram.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_state (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = conn.execute(
            "SELECT value FROM server_state WHERE key = 'fold_from'"
        ).fetchone()
        conn.close()
        assert row is None
