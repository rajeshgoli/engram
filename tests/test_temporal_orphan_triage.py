"""Tests for temporal orphan triage (#25).

Covers: git-based orphan detection, temporal prompt rendering,
fold_from lifecycle (set/use/clear), legacy schema migration,
zero-work fold_from clearing.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engram.server.db import ServerDB


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / ".engram" / "engram.db"


@pytest.fixture()
def db(db_path: Path) -> ServerDB:
    return ServerDB(db_path)


# ==================================================================
# 1. ServerDB: fold_from accessors
# ==================================================================


class TestFoldFromAccessors:
    def test_get_fold_from_default_none(self, db: ServerDB) -> None:
        assert db.get_fold_from() is None

    def test_set_and_get_fold_from(self, db: ServerDB) -> None:
        db.set_fold_from("2026-01-01")
        assert db.get_fold_from() == "2026-01-01"

    def test_clear_fold_from(self, db: ServerDB) -> None:
        db.set_fold_from("2026-01-01")
        db.clear_fold_from()
        assert db.get_fold_from() is None

    def test_set_fold_from_overwrites(self, db: ServerDB) -> None:
        db.set_fold_from("2026-01-01")
        db.set_fold_from("2026-02-15")
        assert db.get_fold_from() == "2026-02-15"

    def test_fold_from_column_exists(self, db_path: Path) -> None:
        """Verify the fold_from column is in the schema."""
        ServerDB(db_path)
        conn = sqlite3.connect(str(db_path))
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(server_state)").fetchall()
        }
        conn.close()
        assert "fold_from" in cols

    def test_fold_from_survives_reinit(self, db_path: Path) -> None:
        """Re-initializing ServerDB preserves fold_from."""
        db1 = ServerDB(db_path)
        db1.set_fold_from("2026-01-01")
        db2 = ServerDB(db_path)
        assert db2.get_fold_from() == "2026-01-01"


# ==================================================================
# 2. Legacy schema migration
# ==================================================================


class TestLegacySchemaDetection:
    def _create_legacy_table(self, db_path: Path, fold_from: str | None = None) -> None:
        """Create the legacy key-value server_state table."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE server_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        if fold_from:
            conn.execute(
                "INSERT INTO server_state (key, value) VALUES ('fold_from', ?)",
                (fold_from,),
            )
        conn.commit()
        conn.close()

    def test_detects_and_rebuilds_legacy_schema(self, db_path: Path) -> None:
        """Legacy key-value table is replaced with singleton-row schema."""
        self._create_legacy_table(db_path, "2026-01-01")
        db = ServerDB(db_path)

        # Verify schema is now singleton-row
        conn = sqlite3.connect(str(db_path))
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(server_state)").fetchall()
        }
        conn.close()
        assert "id" in cols
        assert "key" not in cols

    def test_preserves_fold_from_across_migration(self, db_path: Path) -> None:
        """fold_from value survives legacy → singleton migration."""
        self._create_legacy_table(db_path, "2026-01-15")
        db = ServerDB(db_path)
        assert db.get_fold_from() == "2026-01-15"

    def test_legacy_table_without_fold_from(self, db_path: Path) -> None:
        """Legacy table with no fold_from key migrates cleanly."""
        self._create_legacy_table(db_path, fold_from=None)
        db = ServerDB(db_path)
        assert db.get_fold_from() is None
        # Should still be functional
        state = db.get_server_state()
        assert state["buffer_chars_total"] == 0

    def test_singleton_schema_unaffected(self, db_path: Path) -> None:
        """An existing singleton schema is not touched by migration logic."""
        db1 = ServerDB(db_path)
        db1.set_fold_from("2026-03-01")
        db1.add_buffer_item("test.md", "doc", 100)

        # Re-init — should not lose data
        db2 = ServerDB(db_path)
        assert db2.get_fold_from() == "2026-03-01"
        assert len(db2.get_buffer_items()) == 1


# ==================================================================
# 3. migrate.py set_fold_marker uses ServerDB
# ==================================================================


class TestSetFoldMarker:
    def test_set_fold_marker_creates_singleton_schema(self, db_path: Path) -> None:
        from engram.migrate import set_fold_marker

        set_fold_marker(db_path, date(2026, 1, 1))

        # Verify singleton schema (not key-value)
        conn = sqlite3.connect(str(db_path))
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(server_state)").fetchall()
        }
        conn.close()
        assert "id" in cols
        assert "key" not in cols

    def test_set_fold_marker_readable_by_server_db(self, db_path: Path) -> None:
        from engram.migrate import set_fold_marker

        set_fold_marker(db_path, date(2026, 2, 10))
        db = ServerDB(db_path)
        assert db.get_fold_from() == "2026-02-10"


# ==================================================================
# 4. Git-based file existence checking
# ==================================================================


class TestGitHelpers:
    def test_resolve_ref_commit_returns_hash(self, tmp_path: Path) -> None:
        """In a real git repo, _resolve_ref_commit returns a commit hash."""
        from engram.fold.chunker import _resolve_ref_commit

        # Create a minimal git repo with a commit
        _init_git_repo(tmp_path)

        commit = _resolve_ref_commit(tmp_path, "2099-12-31")
        assert commit is not None
        assert len(commit) == 40  # Full SHA

    def test_resolve_ref_commit_returns_none_for_ancient_date(
        self, tmp_path: Path,
    ) -> None:
        from engram.fold.chunker import _resolve_ref_commit

        _init_git_repo(tmp_path)
        # Date before the repo existed
        assert _resolve_ref_commit(tmp_path, "1900-01-01") is None

    def test_resolve_ref_commit_returns_none_for_non_repo(
        self, tmp_path: Path,
    ) -> None:
        from engram.fold.chunker import _resolve_ref_commit

        assert _resolve_ref_commit(tmp_path, "2026-01-01") is None

    def test_file_exists_at_commit(self, tmp_path: Path) -> None:
        from engram.fold.chunker import _file_exists_at_commit

        commit = _init_git_repo(tmp_path)

        assert _file_exists_at_commit(tmp_path, commit, "hello.txt")
        assert not _file_exists_at_commit(tmp_path, commit, "missing.txt")

    def test_file_exists_at_commit_tracks_rename(self, tmp_path: Path) -> None:
        """After renaming, old name exists at old commit, new name at new."""
        from engram.fold.chunker import _file_exists_at_commit

        import subprocess

        commit1 = _init_git_repo(tmp_path)

        # Rename the file
        old = tmp_path / "hello.txt"
        new = tmp_path / "goodbye.txt"
        old.rename(new)
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "rename"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path), capture_output=True, text=True, check=True,
        )
        commit2 = result.stdout.strip()

        # Old commit has old name
        assert _file_exists_at_commit(tmp_path, commit1, "hello.txt")
        assert not _file_exists_at_commit(tmp_path, commit1, "goodbye.txt")

        # New commit has new name
        assert not _file_exists_at_commit(tmp_path, commit2, "hello.txt")
        assert _file_exists_at_commit(tmp_path, commit2, "goodbye.txt")


# ==================================================================
# 5. Orphan detection with temporal reference
# ==================================================================


class TestTemporalOrphanDetection:
    def test_orphan_detected_without_ref_commit(self, tmp_path: Path) -> None:
        """Steady-state: missing files flagged as orphans (filesystem check)."""
        from engram.fold.chunker import _find_orphaned_concepts

        concepts = tmp_path / "concepts.md"
        concepts.write_text(
            "# Concept Registry\n\n"
            "## C001: Widget (ACTIVE)\n"
            "- **Code:** `src/widget.py`\n"
        )
        # src/widget.py does NOT exist

        orphans = _find_orphaned_concepts(concepts, tmp_path)
        assert len(orphans) == 1
        assert orphans[0]["id"] == "C001"

    def test_no_orphan_when_file_exists_at_ref_commit(self, tmp_path: Path) -> None:
        """With ref_commit, file present at that commit is NOT an orphan."""
        from engram.fold.chunker import _find_orphaned_concepts

        import subprocess

        # Set up git repo with src/widget.py
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "widget.py").write_text("# widget")
        commit = _init_git_repo(tmp_path, files=["src/widget.py"])

        # Now delete the file from the filesystem (simulating a rename after fold_from)
        (tmp_path / "src" / "widget.py").unlink()

        concepts = tmp_path / "concepts.md"
        concepts.write_text(
            "# Concept Registry\n\n"
            "## C001: Widget (ACTIVE)\n"
            "- **Code:** `src/widget.py`\n"
        )

        # Without ref_commit — filesystem check → orphan
        orphans = _find_orphaned_concepts(concepts, tmp_path)
        assert len(orphans) == 1

        # With ref_commit — file existed at that commit → NOT orphan
        orphans = _find_orphaned_concepts(concepts, tmp_path, ref_commit=commit)
        assert len(orphans) == 0

    def test_orphan_when_file_missing_at_ref_commit(self, tmp_path: Path) -> None:
        """With ref_commit, file missing at that commit IS an orphan."""
        from engram.fold.chunker import _find_orphaned_concepts

        commit = _init_git_repo(tmp_path)

        concepts = tmp_path / "concepts.md"
        concepts.write_text(
            "# Concept Registry\n\n"
            "## C001: Widget (ACTIVE)\n"
            "- **Code:** `src/widget.py`\n"
        )

        orphans = _find_orphaned_concepts(concepts, tmp_path, ref_commit=commit)
        assert len(orphans) == 1
        assert orphans[0]["id"] == "C001"


# ==================================================================
# 6. scan_drift threading
# ==================================================================


class TestScanDriftFoldFrom:
    def test_scan_drift_passes_fold_from(self, tmp_path: Path) -> None:
        """scan_drift with fold_from uses git-based detection."""
        from engram.fold.chunker import scan_drift

        import subprocess

        # Set up a project with config
        _setup_project(tmp_path)

        # Create a git repo with the source file
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "widget.py").write_text("# widget")
        _init_git_repo(tmp_path, files=["src/widget.py", ".engram/config.yaml",
                                         "docs/decisions/concept_registry.md",
                                         "docs/decisions/timeline.md",
                                         "docs/decisions/epistemic_state.md",
                                         "docs/decisions/workflow_registry.md",
                                         "docs/decisions/concept_graveyard.md",
                                         "docs/decisions/epistemic_graveyard.md"])

        # Delete the file from filesystem
        (tmp_path / "src" / "widget.py").unlink()

        # Add orphan concept to registry
        concepts_path = tmp_path / "docs" / "decisions" / "concept_registry.md"
        concepts_path.write_text(
            "# Concept Registry\n\n"
            "## C001: Widget (ACTIVE)\n"
            "- **Code:** `src/widget.py`\n"
        )

        from engram.config import load_config
        config = load_config(tmp_path)
        # Lower threshold so it triggers
        config["thresholds"]["orphan_triage"] = 0

        # Without fold_from — filesystem says file missing → orphan
        drift_no_fold = scan_drift(config, tmp_path)
        assert len(drift_no_fold.orphaned_concepts) == 1

        # With fold_from — git says file existed → NOT orphan
        drift_with_fold = scan_drift(config, tmp_path, fold_from="2099-12-31")
        assert len(drift_with_fold.orphaned_concepts) == 0


# ==================================================================
# 7. Temporal context in triage prompt
# ==================================================================


class TestTriagePromptTemporalContext:
    def test_no_temporal_block_without_ref_commit(self, tmp_path: Path) -> None:
        from engram.fold.prompt import render_triage_input
        from engram.fold.chunker import DriftReport

        drift = DriftReport(
            orphaned_concepts=[{"name": "C001: Widget", "id": "C001", "paths": ["src/w.py"]}],
        )
        doc_paths = _fake_doc_paths(tmp_path)

        output = render_triage_input(
            drift_type="orphan_triage",
            drift_report=drift,
            chunk_id=1,
            doc_paths=doc_paths,
        )
        assert "Temporal Context" not in output

    def test_temporal_block_with_ref_commit(self, tmp_path: Path) -> None:
        from engram.fold.prompt import render_triage_input
        from engram.fold.chunker import DriftReport

        drift = DriftReport(
            orphaned_concepts=[{"name": "C001: Widget", "id": "C001", "paths": ["src/w.py"]}],
        )
        doc_paths = _fake_doc_paths(tmp_path)

        output = render_triage_input(
            drift_type="orphan_triage",
            drift_report=drift,
            chunk_id=1,
            doc_paths=doc_paths,
            ref_commit="abc123def456789012345678901234567890abcd",
            ref_date="2026-01-01",
        )
        assert "Temporal Context" in output
        assert "2026-01-01" in output
        assert "abc123def456" in output
        assert "git worktree add" in output

    def test_temporal_block_not_in_contested_review(self, tmp_path: Path) -> None:
        """Temporal context does not appear in contested review."""
        from engram.fold.prompt import render_triage_input
        from engram.fold.chunker import DriftReport

        drift = DriftReport(
            contested_claims=[{
                "name": "E001: Claim", "id": "E001",
                "days_old": 30, "last_date": "2025-12-01",
            }],
        )
        doc_paths = _fake_doc_paths(tmp_path)

        output = render_triage_input(
            drift_type="contested_review",
            drift_report=drift,
            chunk_id=1,
            doc_paths=doc_paths,
            ref_commit="abc123def456",
            ref_date="2026-01-01",
        )
        assert "Temporal Context" not in output

    def test_epistemic_audit_includes_evidence_gate_instructions(self, tmp_path: Path) -> None:
        """Epistemic audit includes temporal context and evidence gate instructions."""
        from engram.fold.prompt import render_triage_input
        from engram.fold.chunker import DriftReport

        drift = DriftReport(
            epistemic_audit=[{
                "name": "E008: Harness Phase 0 completion (believed)",
                "id": "E008",
                "days_old": 72,
                "last_date": "2025-12-11",
            }],
        )
        doc_paths = _fake_doc_paths(tmp_path)

        output = render_triage_input(
            drift_type="epistemic_audit",
            drift_report=drift,
            chunk_id=13,
            doc_paths=doc_paths,
            ref_commit="abc123def456789012345678901234567890abcd",
            ref_date="2026-01-01",
        )
        assert "Temporal Context" in output
        assert "2026-01-01" in output
        assert "abc123def456" in output
        assert "git worktree add /tmp/engram-epistemic-abc123de" in output
        assert "Evidence@<commit>" in output
        assert "Do NOT use generic lines like `reaffirmed -> believed`." in output
        assert "/epistemic_state/history/E{NNN}.md" in output
        assert "/epistemic_state/current/E{NNN}.md" in output

    def test_epistemic_audit_uses_split_paths_even_when_legacy_files_exist(self, tmp_path: Path) -> None:
        from engram.fold.prompt import render_triage_input
        from engram.fold.chunker import DriftReport

        drift = DriftReport(
            epistemic_audit=[{
                "name": "E008: Harness Phase 0 completion (believed)",
                "id": "E008",
                "days_old": 72,
                "last_date": "2025-12-11",
            }],
        )
        doc_paths = _fake_doc_paths(tmp_path)
        legacy = tmp_path / "docs" / "decisions" / "epistemic_state" / "E008.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("## E008: legacy\n")

        output = render_triage_input(
            drift_type="epistemic_audit",
            drift_report=drift,
            chunk_id=13,
            doc_paths=doc_paths,
            ref_commit="abc123def456789012345678901234567890abcd",
            ref_date="2026-01-01",
        )
        assert "/epistemic_state/history/E{NNN}.md" in output
        assert "/epistemic_state/current/E{NNN}.md" in output
        assert "/epistemic_state/E{NNN}.md" not in output


# ==================================================================
# 8. fold_from lifecycle — set → use → clear
# ==================================================================


class TestFoldFromLifecycle:
    def test_forward_fold_clears_fold_from_on_empty_queue(
        self, tmp_path: Path,
    ) -> None:
        """Early return (empty queue after date filter) clears fold_from."""
        _setup_project(tmp_path)
        _init_git_repo(tmp_path, files=[
            ".engram/config.yaml",
            "docs/decisions/timeline.md",
            "docs/decisions/concept_registry.md",
            "docs/decisions/epistemic_state.md",
            "docs/decisions/workflow_registry.md",
            "docs/decisions/concept_graveyard.md",
            "docs/decisions/epistemic_graveyard.md",
        ])

        db_path = tmp_path / ".engram" / "engram.db"
        db = ServerDB(db_path)
        db.set_fold_from("2026-01-01")

        # Create an empty queue file (or one where all entries predate fold_from)
        queue_file = tmp_path / ".engram" / "queue.jsonl"
        queue_file.write_text("")

        # Mock build_queue to return an empty list
        with patch("engram.bootstrap.fold.build_queue", return_value=[]):
            from engram.bootstrap.fold import forward_fold
            from engram.config import load_config

            result = forward_fold(tmp_path, date(2099, 1, 1))

        assert result is True
        assert db.get_fold_from() is None

    def test_forward_fold_clears_fold_from_on_success(
        self, tmp_path: Path,
    ) -> None:
        """Normal completion clears fold_from."""
        import json

        _setup_project(tmp_path)
        _init_git_repo(tmp_path, files=[
            ".engram/config.yaml",
            "docs/decisions/timeline.md",
            "docs/decisions/concept_registry.md",
            "docs/decisions/epistemic_state.md",
            "docs/decisions/workflow_registry.md",
            "docs/decisions/concept_graveyard.md",
            "docs/decisions/epistemic_graveyard.md",
        ])

        db_path = tmp_path / ".engram" / "engram.db"
        db = ServerDB(db_path)
        db.set_fold_from("2026-01-01")

        # Create a queue with one entry
        queue_file = tmp_path / ".engram" / "queue.jsonl"
        entry = {
            "path": "docs/working/test.md",
            "type": "doc",
            "date": "2026-01-15T00:00:00",
            "chars": 100,
            "pass": "initial",
        }
        queue_file.write_text(json.dumps(entry) + "\n")
        # Create the referenced file
        working_dir = tmp_path / "docs" / "working"
        working_dir.mkdir(parents=True, exist_ok=True)
        (working_dir / "test.md").write_text("test content")

        with (
            patch("engram.bootstrap.fold.build_queue") as mock_bq,
            patch("engram.bootstrap.fold._dispatch_and_validate", return_value=True),
        ):
            mock_bq.return_value = [entry]

            from engram.bootstrap.fold import forward_fold

            result = forward_fold(tmp_path, date(2026, 1, 1))

        assert result is True
        assert db.get_fold_from() is None


# ==================================================================
# Helpers
# ==================================================================


def _init_git_repo(
    root: Path,
    files: list[str] | None = None,
) -> str:
    """Initialize a git repo, add files, make initial commit. Returns commit hash."""
    import subprocess

    subprocess.run(
        ["git", "init"],
        cwd=str(root), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(root), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(root), capture_output=True, check=True,
    )

    if files is None:
        # Create a default file
        (root / "hello.txt").write_text("hello")
        files = ["hello.txt"]

    for f in files:
        p = root / f
        if p.exists():
            subprocess.run(
                ["git", "add", f],
                cwd=str(root), capture_output=True, check=True,
            )
    subprocess.run(
        ["git", "commit", "-m", "initial", "--allow-empty"],
        cwd=str(root), capture_output=True, check=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _setup_project(root: Path) -> None:
    """Create a minimal engram project structure."""
    engram_dir = root / ".engram"
    engram_dir.mkdir(parents=True, exist_ok=True)

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

    docs_dir = root / "docs" / "decisions"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "timeline.md").write_text("# Timeline\n")
    (docs_dir / "concept_registry.md").write_text("# Concept Registry\n")
    (docs_dir / "epistemic_state.md").write_text("# Epistemic State\n")
    (docs_dir / "workflow_registry.md").write_text("# Workflow Registry\n")
    (docs_dir / "concept_graveyard.md").write_text("# Concept Graveyard\n")
    (docs_dir / "epistemic_graveyard.md").write_text("# Epistemic Graveyard\n")


def _fake_doc_paths(root: Path) -> dict[str, Path]:
    """Return a doc_paths dict pointing to tmp_path-based paths."""
    docs = root / "docs" / "decisions"
    docs.mkdir(parents=True, exist_ok=True)
    return {
        "timeline": docs / "timeline.md",
        "concepts": docs / "concept_registry.md",
        "epistemic": docs / "epistemic_state.md",
        "workflows": docs / "workflow_registry.md",
        "concept_graveyard": docs / "concept_graveyard.md",
        "epistemic_graveyard": docs / "epistemic_graveyard.md",
    }
