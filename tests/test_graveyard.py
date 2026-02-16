"""Tests for engram.compact — graveyard moves, STUBs, correction blocks,
timeline compaction, and orphan detection.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from engram.compact.graveyard import (
    append_correction_block,
    compact_living_doc,
    find_orphaned_concepts,
    generate_stub,
    move_to_graveyard,
)
from engram.compact.timeline import compact_timeline
from engram.parse import Section, is_stub, parse_sections


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONCEPT_DEAD = Section(
    heading="## C042: proximity_pruning (DEAD)",
    status="dead",
    start=0,
    end=5,
    text=(
        "## C042: proximity_pruning (DEAD)\n"
        "- **Code:** `src/pruning.py`\n"
        "- **Rationale:** No longer needed after refactor.\n"
        "- Related: C010, E005"
    ),
)

CONCEPT_EVOLVED = Section(
    heading="## C015: fractal_detector (EVOLVED \u2192 C089)",
    status="evolved",
    start=0,
    end=4,
    text=(
        "## C015: fractal_detector (EVOLVED \u2192 C089)\n"
        "- **Code:** `src/detect.py`\n"
        "- Replaced by multi-scale detector C089."
    ),
)

EPISTEMIC_REFUTED = Section(
    heading="## E007: mean_reversion_dominant (refuted)",
    status="refuted",
    start=0,
    end=4,
    text=(
        "## E007: mean_reversion_dominant (refuted)\n"
        "- **Evidence:** Backtesting showed momentum dominates in trending markets.\n"
        "- Refuted by E012."
    ),
)


# ---------------------------------------------------------------------------
# generate_stub
# ---------------------------------------------------------------------------

class TestGenerateStub:
    def test_concept_dead_stub(self):
        stub = generate_stub(CONCEPT_DEAD, "concepts", "concept_graveyard.md")
        assert stub == "## C042: proximity_pruning (DEAD) \u2192 concept_graveyard.md#C042"
        assert is_stub(stub)

    def test_concept_evolved_stub(self):
        stub = generate_stub(CONCEPT_EVOLVED, "concepts", "concept_graveyard.md")
        assert stub == "## C015: fractal_detector (EVOLVED \u2192 C089) \u2192 concept_graveyard.md#C015"
        assert is_stub(stub)

    def test_epistemic_refuted_stub(self):
        stub = generate_stub(EPISTEMIC_REFUTED, "epistemic", "epistemic_graveyard.md")
        assert stub == "## E007: mean_reversion_dominant (refuted) \u2192 epistemic_graveyard.md#E007"
        assert is_stub(stub)

    def test_no_stable_id_raises(self):
        bad_section = Section(
            heading="## Some Heading Without ID",
            status="dead",
            start=0, end=1,
            text="## Some Heading Without ID",
        )
        with pytest.raises(ValueError, match="no stable ID"):
            generate_stub(bad_section, "concepts", "concept_graveyard.md")


# ---------------------------------------------------------------------------
# move_to_graveyard
# ---------------------------------------------------------------------------

class TestMoveToGraveyard:
    def test_appends_to_new_graveyard(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        stub = move_to_graveyard(CONCEPT_DEAD, "concepts", gy_path)

        assert is_stub(stub)
        assert "C042" in stub
        assert gy_path.exists()

        gy_content = gy_path.read_text()
        assert "## C042: proximity_pruning (DEAD)" in gy_content
        assert "src/pruning.py" in gy_content

    def test_appends_to_existing_graveyard(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        gy_path.write_text("## C001: old_entry (DEAD)\nOld content.\n")

        move_to_graveyard(CONCEPT_DEAD, "concepts", gy_path)

        gy_content = gy_path.read_text()
        # Both entries present
        assert "C001" in gy_content
        assert "C042" in gy_content
        # Separated by blank line
        assert "\n\n## C042:" in gy_content

    def test_evolved_entry(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        stub = move_to_graveyard(CONCEPT_EVOLVED, "concepts", gy_path)

        assert "EVOLVED" in stub
        assert "C015" in stub
        gy_content = gy_path.read_text()
        assert "fractal_detector" in gy_content

    def test_epistemic_refuted(self, tmp_path: Path):
        gy_path = tmp_path / "epistemic_graveyard.md"
        stub = move_to_graveyard(EPISTEMIC_REFUTED, "epistemic", gy_path)

        assert "refuted" in stub
        assert "E007" in stub
        assert gy_path.read_text().startswith("## E007:")

    def test_wrong_doc_type_raises(self, tmp_path: Path):
        gy_path = tmp_path / "graveyard.md"
        with pytest.raises(ValueError, match="Unknown doc_type"):
            move_to_graveyard(CONCEPT_DEAD, "workflows", gy_path)

    def test_wrong_status_raises(self, tmp_path: Path):
        active_section = Section(
            heading="## C050: active_thing (ACTIVE)",
            status="active",
            start=0, end=1,
            text="## C050: active_thing (ACTIVE)\nContent.",
        )
        gy_path = tmp_path / "concept_graveyard.md"
        with pytest.raises(ValueError, match="not a graveyard status"):
            move_to_graveyard(active_section, "concepts", gy_path)


# ---------------------------------------------------------------------------
# append_correction_block
# ---------------------------------------------------------------------------

class TestCorrectionBlock:
    def test_basic_correction(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        gy_path.write_text("## C012: some_concept (DEAD)\nOriginal content.\n")

        append_correction_block(
            gy_path,
            entry_id="C012",
            old_status="DEAD",
            new_status="EVOLVED",
            target="C089",
            correction_date=date(2026, 2, 20),
        )

        content = gy_path.read_text()
        assert "## C012 CORRECTION (2026-02-20)" in content
        assert "DEAD \u2192 EVOLVED \u2192 C089" in content
        assert "concept_registry.md" in content
        # Original entry preserved
        assert "Original content." in content

    def test_correction_without_target(self, tmp_path: Path):
        gy_path = tmp_path / "epistemic_graveyard.md"
        gy_path.write_text("## E005: claim (refuted)\nEvidence.\n")

        append_correction_block(
            gy_path,
            entry_id="E005",
            old_status="refuted",
            new_status="believed",
            correction_date=date(2026, 3, 1),
        )

        content = gy_path.read_text()
        assert "## E005 CORRECTION (2026-03-01)" in content
        assert "refuted \u2192 believed" in content
        assert "epistemic_state.md" in content

    def test_multiple_corrections_append(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        gy_path.write_text("## C012: concept (DEAD)\nOriginal.\n")

        append_correction_block(
            gy_path, "C012", "DEAD", "EVOLVED", "C089",
            correction_date=date(2026, 2, 20),
        )
        append_correction_block(
            gy_path, "C012", "EVOLVED", "DEAD", None,
            correction_date=date(2026, 3, 15),
        )

        content = gy_path.read_text()
        assert content.count("CORRECTION") == 2
        assert "2026-02-20" in content
        assert "2026-03-15" in content

    def test_correction_on_new_file(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        # File doesn't exist yet
        append_correction_block(
            gy_path, "C099", "DEAD", "ACTIVE",
            correction_date=date(2026, 1, 1),
        )
        assert gy_path.exists()
        content = gy_path.read_text()
        assert "## C099 CORRECTION" in content


# ---------------------------------------------------------------------------
# compact_living_doc
# ---------------------------------------------------------------------------

class TestCompactLivingDoc:
    def test_compacts_dead_entries(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        content = (
            "# Concept Registry\n"
            "\n"
            "## C001: active_concept (ACTIVE)\n"
            "- **Code:** `src/active.py`\n"
            "\n"
            "## C042: proximity_pruning (DEAD)\n"
            "- **Code:** `src/pruning.py`\n"
            "- **Rationale:** No longer needed.\n"
            "- Related: C001\n"
            "\n"
            "## C050: another_active (ACTIVE)\n"
            "- **Code:** `src/another.py`\n"
        )

        new_content, chars_saved = compact_living_doc(content, "concepts", gy_path)

        assert chars_saved > 0
        # STUB replaces the dead entry
        assert "C042" in new_content
        assert "\u2192 concept_graveyard.md#C042" in new_content
        # Active entries preserved
        assert "src/active.py" in new_content
        assert "src/another.py" in new_content
        # Dead entry's full content NOT in living doc
        assert "No longer needed" not in new_content
        # But IS in graveyard
        gy_content = gy_path.read_text()
        assert "No longer needed" in gy_content

    def test_skips_existing_stubs(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        content = (
            "# Concept Registry\n"
            "\n"
            "## C001: old_stub (DEAD) \u2192 concept_graveyard.md#C001\n"
            "\n"
            "## C002: active (ACTIVE)\n"
            "- **Code:** `src/a.py`\n"
        )

        new_content, chars_saved = compact_living_doc(content, "concepts", gy_path)

        assert chars_saved == 0
        assert "C001" in new_content
        # Graveyard not written to
        assert not gy_path.exists()

    def test_compacts_epistemic_refuted(self, tmp_path: Path):
        gy_path = tmp_path / "epistemic_graveyard.md"
        content = (
            "# Epistemic State\n"
            "\n"
            "## E001: claim_one (believed)\n"
            "- **Evidence:** Strong evidence.\n"
            "\n"
            "## E007: mean_reversion (refuted)\n"
            "- **Evidence:** Backtesting showed otherwise.\n"
            "- Refuted by E012.\n"
        )

        new_content, chars_saved = compact_living_doc(content, "epistemic", gy_path)

        assert chars_saved > 0
        assert "\u2192 epistemic_graveyard.md#E007" in new_content
        assert "Strong evidence" in new_content
        assert "Backtesting showed otherwise" not in new_content

    def test_no_eligible_entries(self, tmp_path: Path):
        gy_path = tmp_path / "concept_graveyard.md"
        content = (
            "# Concept Registry\n"
            "\n"
            "## C001: active (ACTIVE)\n"
            "- **Code:** `src/a.py`\n"
        )

        new_content, chars_saved = compact_living_doc(content, "concepts", gy_path)

        assert chars_saved == 0
        assert new_content == content

    def test_wrong_doc_type_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Unknown doc_type"):
            compact_living_doc("content", "timeline", tmp_path / "gy.md")


# ---------------------------------------------------------------------------
# find_orphaned_concepts
# ---------------------------------------------------------------------------

class TestFindOrphanedConcepts:
    def test_finds_orphans_with_all_files_missing(self, tmp_path: Path):
        registry = (
            "## C001: some_concept (ACTIVE)\n"
            "- **Code:** `src/foo.py`, `src/bar.py`\n"
        )
        # Neither file exists under tmp_path
        orphans = find_orphaned_concepts(registry, tmp_path)

        assert len(orphans) == 1
        assert orphans[0]["id"] == "C001"
        assert orphans[0]["name"] == "some_concept"
        assert set(orphans[0]["paths"]) == {"src/foo.py", "src/bar.py"}

    def test_skips_when_some_files_exist(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("# exists")

        registry = (
            "## C001: some_concept (ACTIVE)\n"
            "- **Code:** `src/foo.py`, `src/bar.py`\n"
        )
        orphans = find_orphaned_concepts(registry, tmp_path)

        # Not ALL files missing, so not an orphan
        assert len(orphans) == 0

    def test_skips_dead_entries(self, tmp_path: Path):
        registry = (
            "## C001: dead_concept (DEAD)\n"
            "- **Code:** `src/gone.py`\n"
        )
        orphans = find_orphaned_concepts(registry, tmp_path)
        assert len(orphans) == 0

    def test_skips_stubs(self, tmp_path: Path):
        registry = (
            "## C001: old_stub (DEAD) \u2192 concept_graveyard.md#C001\n"
        )
        orphans = find_orphaned_concepts(registry, tmp_path)
        assert len(orphans) == 0

    def test_skips_entries_without_code_field(self, tmp_path: Path):
        registry = (
            "## C001: no_code_concept (ACTIVE)\n"
            "- **Description:** Something without code.\n"
        )
        orphans = find_orphaned_concepts(registry, tmp_path)
        assert len(orphans) == 0

    def test_multiple_orphans(self, tmp_path: Path):
        registry = (
            "## C001: orphan_one (ACTIVE)\n"
            "- **Code:** `src/a.py`\n"
            "\n"
            "## C002: not_orphan (ACTIVE)\n"
            "- **Description:** No code field.\n"
            "\n"
            "## C003: orphan_two (ACTIVE)\n"
            "- **Code:** `tests/test_b.py`\n"
        )
        orphans = find_orphaned_concepts(registry, tmp_path)
        assert len(orphans) == 2
        ids = {o["id"] for o in orphans}
        assert ids == {"C001", "C003"}

    def test_custom_source_patterns(self, tmp_path: Path):
        registry = (
            "## C001: custom_concept (ACTIVE)\n"
            "- **Code:** `packages/core/mod.ts`\n"
        )
        # Default patterns won't match packages/
        orphans = find_orphaned_concepts(registry, tmp_path)
        assert len(orphans) == 0

        # Custom pattern that matches
        orphans = find_orphaned_concepts(
            registry, tmp_path,
            source_patterns=[r'packages/[\w/._-]+\.ts'],
        )
        assert len(orphans) == 1


# ---------------------------------------------------------------------------
# compact_timeline
# ---------------------------------------------------------------------------

class TestCompactTimeline:
    def _make_timeline(self, phases: list[tuple[str, str]], padding: int = 0) -> str:
        """Build a timeline doc from (heading, body) pairs."""
        parts = ["# Project Timeline\n"]
        for heading, body in phases:
            parts.append(f"## Phase: {heading}")
            parts.append(body)
            parts.append("")
        content = "\n".join(parts)
        if padding > 0 and len(content) < padding:
            content += "\n" + "x" * (padding - len(content))
        return content

    def test_no_compaction_below_threshold(self):
        content = self._make_timeline([
            ("Alpha (Jan 2020 \u2013 Jun 2020)", "Built the thing. Refs: C001, E001."),
        ])
        new_content, saved = compact_timeline(content, threshold_chars=100_000)
        assert saved == 0
        assert new_content == content

    def test_compacts_old_phases(self):
        old_phase_body = "Detailed narrative about early development.\n" * 50
        old_phase_body += "References: C001, C002, E005.\n"

        content = self._make_timeline(
            [
                ("Foundation (Jan 2020 \u2013 Jun 2020)", old_phase_body),
                ("Growth (Jan 2026 \u2013 Feb 2026)", "Recent work on C010."),
            ],
            padding=55_000,
        )

        new_content, saved = compact_timeline(
            content,
            threshold_chars=50_000,
            age_months=6,
            reference_date=date(2026, 2, 15),
        )

        assert saved > 0
        # Old phase is compacted
        assert "Detailed narrative" not in new_content or len(new_content) < len(content)
        # ID references preserved
        for ref_id in ["C001", "C002", "E005"]:
            assert ref_id in new_content
        # Recent phase untouched
        assert "Recent work on C010" in new_content

    def test_preserves_recent_phases(self):
        content = self._make_timeline(
            [
                ("Recent (Jan 2026 \u2013 Feb 2026)", "Very recent work.\n" * 100),
            ],
            padding=55_000,
        )

        new_content, saved = compact_timeline(
            content,
            threshold_chars=50_000,
            age_months=6,
            reference_date=date(2026, 2, 15),
        )

        # Phase is too recent to compact
        assert saved == 0

    def test_iso_date_parsing(self):
        old_body = "Old content.\n" * 100 + "Ref C001.\n"
        content = self._make_timeline(
            [
                ("Init (2020-01-15 \u2013 2020-06-30)", old_body),
                ("Current (2026-01-01 \u2013 2026-02-15)", "Now.\n"),
            ],
            padding=55_000,
        )

        new_content, saved = compact_timeline(
            content,
            threshold_chars=50_000,
            reference_date=date(2026, 2, 15),
        )

        assert saved > 0
        assert "C001" in new_content

    def test_no_date_phases_kept(self):
        """Phases without parseable dates are kept as-is."""
        content = self._make_timeline(
            [
                ("Undated Phase", "Some content.\n" * 100),
            ],
            padding=55_000,
        )

        new_content, saved = compact_timeline(
            content,
            threshold_chars=50_000,
            reference_date=date(2026, 2, 15),
        )

        assert saved == 0
