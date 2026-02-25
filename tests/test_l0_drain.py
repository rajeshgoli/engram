"""Tests for L0 briefing regeneration on queue drain (#24).

Covers:
- l0_stale DB column + methods
- queue_is_empty drain predicate
- Dispatcher marks L0 stale (not regen) on dispatch/recovery
- Crash-window ordering: stale BEFORE committed
- Bootstrap fold/seed L0 regen at completion
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

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


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Minimal engram project for testing."""
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

    docs_dir = tmp_path / "docs" / "decisions"
    docs_dir.mkdir(parents=True)
    (docs_dir / "timeline.md").write_text("# Timeline\n")
    (docs_dir / "concept_registry.md").write_text("# Concept Registry\n")
    (docs_dir / "epistemic_state.md").write_text("# Epistemic State\n")
    (docs_dir / "workflow_registry.md").write_text("# Workflow Registry\n")
    (docs_dir / "concept_graveyard.md").write_text("# Concept Graveyard\n")
    (docs_dir / "epistemic_graveyard.md").write_text("# Epistemic Graveyard\n")

    return tmp_path


@pytest.fixture()
def config(project: Path) -> dict:
    from engram.config import load_config
    return load_config(project)


# ==================================================================
# DB: l0_stale column and methods
# ==================================================================


class TestL0StaleDB:
    def test_default_not_stale(self, db: ServerDB) -> None:
        assert db.is_l0_stale() is False

    def test_mark_stale(self, db: ServerDB) -> None:
        db.mark_l0_stale()
        assert db.is_l0_stale() is True

    def test_clear_stale(self, db: ServerDB) -> None:
        db.mark_l0_stale()
        db.clear_l0_stale()
        assert db.is_l0_stale() is False

    def test_mark_stale_idempotent(self, db: ServerDB) -> None:
        db.mark_l0_stale()
        db.mark_l0_stale()
        assert db.is_l0_stale() is True

    def test_column_migration_idempotent(self, db_path: Path) -> None:
        """Re-initializing DB doesn't fail on existing l0_stale column."""
        db1 = ServerDB(db_path)
        db1.mark_l0_stale()
        # Re-init — should not raise
        db2 = ServerDB(db_path)
        assert db2.is_l0_stale() is True

    def test_l0_stale_visible_in_server_state(self, db: ServerDB) -> None:
        db.mark_l0_stale()
        state = db.get_server_state()
        assert state["l0_stale"] == 1


# ==================================================================
# queue_is_empty drain predicate
# ==================================================================


class TestQueueIsEmpty:
    def test_missing_file(self, tmp_path: Path) -> None:
        from engram.fold.chunker import queue_is_empty
        assert queue_is_empty(tmp_path) is True

    def test_empty_file(self, tmp_path: Path) -> None:
        from engram.fold.chunker import queue_is_empty
        engram_dir = tmp_path / ".engram"
        engram_dir.mkdir()
        (engram_dir / "queue.jsonl").write_text("")
        assert queue_is_empty(tmp_path) is True

    def test_whitespace_only(self, tmp_path: Path) -> None:
        from engram.fold.chunker import queue_is_empty
        engram_dir = tmp_path / ".engram"
        engram_dir.mkdir()
        (engram_dir / "queue.jsonl").write_text("\n\n  \n")
        assert queue_is_empty(tmp_path) is True

    def test_non_empty(self, tmp_path: Path) -> None:
        from engram.fold.chunker import queue_is_empty
        engram_dir = tmp_path / ".engram"
        engram_dir.mkdir()
        entry = {"path": "test.md", "type": "doc", "chars": 100, "date": "2026-01-01"}
        (engram_dir / "queue.jsonl").write_text(json.dumps(entry) + "\n")
        assert queue_is_empty(tmp_path) is False


# ==================================================================
# Dispatcher: dispatch() uses mark_l0_stale, not L0 regen
# ==================================================================


class TestDispatcherMarksStale:
    def test_dispatch_marks_l0_stale_not_regen(self, project: Path, config: dict) -> None:
        """Successful dispatch should call mark_l0_stale, not regenerate briefing."""
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        # Mock the internals so dispatch succeeds without a real agent
        with patch.object(dispatcher, "_execute_and_validate", return_value=True), \
             patch("engram.server.dispatcher.next_chunk") as mock_nc:
            mock_chunk = MagicMock()
            mock_chunk.chunk_id = 1
            mock_nc.return_value = mock_chunk

            result = dispatcher.dispatch()

        assert result is True
        assert db.is_l0_stale() is True

    def test_failed_dispatch_does_not_mark_stale(self, project: Path, config: dict) -> None:
        """Failed dispatch should NOT mark L0 stale."""
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        with patch.object(dispatcher, "_execute_and_validate", return_value=False), \
             patch("engram.server.dispatcher.next_chunk") as mock_nc:
            mock_chunk = MagicMock()
            mock_chunk.chunk_id = 1
            mock_nc.return_value = mock_chunk

            result = dispatcher.dispatch()

        assert result is False
        assert db.is_l0_stale() is False


# ==================================================================
# Crash-window ordering: stale BEFORE committed
# ==================================================================


class TestCrashWindowOrdering:
    def test_dispatch_stale_before_committed(self, project: Path, config: dict) -> None:
        """mark_l0_stale() must be called before update_dispatch_state(committed)."""
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        call_order: list[str] = []
        original_mark = db.mark_l0_stale
        original_update = db.update_dispatch_state

        def tracked_mark():
            call_order.append("mark_l0_stale")
            return original_mark()

        def tracked_update(did, state, **kwargs):
            call_order.append(f"update_{state}")
            return original_update(did, state, **kwargs)

        with patch.object(dispatcher, "_execute_and_validate", return_value=True), \
             patch("engram.server.dispatcher.next_chunk") as mock_nc, \
             patch.object(db, "mark_l0_stale", side_effect=tracked_mark), \
             patch.object(db, "update_dispatch_state", side_effect=tracked_update):
            mock_chunk = MagicMock()
            mock_chunk.chunk_id = 1
            mock_nc.return_value = mock_chunk
            dispatcher.dispatch()

        # Verify ordering: validated → stale → committed
        assert "mark_l0_stale" in call_order
        assert "update_committed" in call_order
        stale_idx = call_order.index("mark_l0_stale")
        committed_idx = call_order.index("update_committed")
        assert stale_idx < committed_idx

    def test_recovery_validated_stale_before_committed(self, project: Path, config: dict) -> None:
        """Recovery of validated dispatch: stale before committed."""
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        did = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(did, "dispatched")
        db.update_dispatch_state(did, "validated")
        dispatch = db.get_dispatch(did)

        call_order: list[str] = []
        original_mark = db.mark_l0_stale
        original_update = db.update_dispatch_state

        def tracked_mark():
            call_order.append("mark_l0_stale")
            return original_mark()

        def tracked_update(did, state, **kwargs):
            call_order.append(f"update_{state}")
            return original_update(did, state, **kwargs)

        with patch.object(db, "mark_l0_stale", side_effect=tracked_mark), \
             patch.object(db, "update_dispatch_state", side_effect=tracked_update):
            dispatcher.recover_dispatch(dispatch)

        stale_idx = call_order.index("mark_l0_stale")
        committed_idx = call_order.index("update_committed")
        assert stale_idx < committed_idx


# ==================================================================
# Dispatcher recovery uses mark_l0_stale
# ==================================================================


class TestRecoveryMarksStale:
    def test_recover_validated_marks_stale(self, project: Path, config: dict) -> None:
        """Validated recovery marks L0 stale and transitions to committed."""
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        did = db.create_dispatch(chunk_id=1)
        db.update_dispatch_state(did, "dispatched")
        db.update_dispatch_state(did, "validated")
        dispatch = db.get_dispatch(did)

        result = dispatcher.recover_dispatch(dispatch)

        assert result is True
        assert db.is_l0_stale() is True
        final = db.get_dispatch(did)
        assert final["state"] == "committed"

    def test_recover_dispatched_lint_pass_marks_stale(self, project: Path, config: dict) -> None:
        """Dispatched recovery with passing lint marks L0 stale."""
        from engram.server.dispatcher import Dispatcher

        db = ServerDB(project / ".engram" / "engram.db")
        dispatcher = Dispatcher(config, project, db)

        chunks_dir = project / ".engram" / "chunks"
        chunks_dir.mkdir(parents=True)
        input_path = chunks_dir / "chunk_001_input.md"
        input_path.write_text("test input")

        did = db.create_dispatch(chunk_id=1, input_path=str(input_path))
        db.update_dispatch_state(did, "dispatched")
        dispatch = db.get_dispatch(did)

        result = dispatcher.recover_dispatch(dispatch)

        assert result is True
        assert db.is_l0_stale() is True
        final = db.get_dispatch(did)
        assert final["state"] == "committed"


# ==================================================================
# Bootstrap fold: L0 regen after all chunks
# ==================================================================


class TestForwardFoldL0:
    @patch("engram.bootstrap.fold.regenerate_l0_briefing")
    @patch("engram.bootstrap.fold._dispatch_and_validate")
    @patch("engram.bootstrap.fold.next_chunk")
    @patch("engram.bootstrap.fold.build_queue")
    def test_l0_regen_after_all_chunks(
        self,
        mock_bq: MagicMock,
        mock_nc: MagicMock,
        mock_dv: MagicMock,
        mock_regen: MagicMock,
        project: Path,
    ) -> None:
        """L0 briefing regenerated exactly once after all chunks complete."""
        from engram.bootstrap.fold import forward_fold
        from datetime import date

        mock_bq.return_value = [
            {"date": "2026-02-01T00:00:00Z", "type": "doc", "path": "x.md", "chars": 100},
        ]

        chunk1 = MagicMock(chunk_id=1, chunk_type="fold", items_count=1,
                           date_range="2026-02-01 to 2026-02-01",
                           pre_assigned_ids={}, chunk_chars=100)
        chunk2 = MagicMock(chunk_id=2, chunk_type="fold", items_count=1,
                           date_range="2026-02-02 to 2026-02-02",
                           pre_assigned_ids={}, chunk_chars=100)
        mock_nc.side_effect = [chunk1, chunk2, ValueError("Queue is empty")]
        mock_dv.return_value = True
        mock_regen.return_value = True

        result = forward_fold(project, date(2026, 1, 1))
        assert result is True
        # L0 regen called exactly once at the end, not per-chunk
        mock_regen.assert_called_once()

    @patch("engram.bootstrap.fold.regenerate_l0_briefing")
    @patch("engram.bootstrap.fold._dispatch_and_validate")
    @patch("engram.bootstrap.fold.next_chunk")
    @patch("engram.bootstrap.fold.build_queue")
    def test_no_l0_regen_on_empty_queue(
        self,
        mock_bq: MagicMock,
        mock_nc: MagicMock,
        mock_dv: MagicMock,
        mock_regen: MagicMock,
        project: Path,
    ) -> None:
        """No L0 regen when queue is empty (no chunks processed)."""
        from engram.bootstrap.fold import forward_fold
        from datetime import date

        mock_bq.return_value = []
        result = forward_fold(project, date(2026, 1, 1))
        assert result is True
        mock_regen.assert_not_called()

    @patch("engram.bootstrap.fold.regenerate_l0_briefing")
    @patch("engram.bootstrap.fold._dispatch_and_validate")
    @patch("engram.bootstrap.fold.next_chunk")
    @patch("engram.bootstrap.fold.build_queue")
    def test_no_l0_regen_on_failure(
        self,
        mock_bq: MagicMock,
        mock_nc: MagicMock,
        mock_dv: MagicMock,
        mock_regen: MagicMock,
        project: Path,
    ) -> None:
        """No L0 regen when chunks fail."""
        from engram.bootstrap.fold import forward_fold
        from datetime import date

        mock_bq.return_value = [
            {"date": "2026-02-01T00:00:00Z", "type": "doc", "path": "x.md", "chars": 100},
        ]
        chunk = MagicMock(chunk_id=1, chunk_type="fold", items_count=1,
                          date_range="2026-02-01 to 2026-02-01",
                          pre_assigned_ids={}, chunk_chars=100)
        mock_nc.side_effect = [chunk, ValueError("Queue is empty")]
        mock_dv.return_value = False

        result = forward_fold(project, date(2026, 1, 1))
        assert result is False
        mock_regen.assert_not_called()


# ==================================================================
# Bootstrap seed: L0 regen after dispatch succeeds
# ==================================================================


class TestSeedL0:
    @patch("engram.bootstrap.seed.regenerate_l0_briefing")
    @patch("engram.bootstrap.seed._dispatch_seed_agent")
    def test_seed_regens_l0(self, mock_dispatch: MagicMock, mock_regen: MagicMock, project: Path) -> None:
        """Seed regenerates L0 briefing after successful dispatch."""
        from engram.bootstrap.seed import seed

        mock_dispatch.return_value = True
        mock_regen.return_value = True

        result = seed(project, from_date=None)
        assert result is True
        mock_regen.assert_called_once()

    @patch("engram.bootstrap.seed.regenerate_l0_briefing")
    @patch("engram.bootstrap.seed._dispatch_seed_agent")
    def test_seed_no_l0_on_failure(self, mock_dispatch: MagicMock, mock_regen: MagicMock, project: Path) -> None:
        """Seed does NOT regen L0 when dispatch fails."""
        from engram.bootstrap.seed import seed

        mock_dispatch.return_value = False

        result = seed(project, from_date=None)
        assert result is False
        mock_regen.assert_not_called()


# ==================================================================
# Briefing module: regenerate_l0_briefing
# ==================================================================


class TestRegenerateL0Briefing:
    def test_returns_false_no_target(self, project: Path, config: dict) -> None:
        """Returns False when target file doesn't exist."""
        from engram.server.briefing import regenerate_l0_briefing
        from engram.config import resolve_doc_paths

        config["briefing"] = {"file": "NONEXISTENT.md", "section": "## Briefing"}
        doc_paths = resolve_doc_paths(config, project)
        assert regenerate_l0_briefing(config, project, doc_paths) is False

    @patch("engram.server.briefing._generate_briefing", return_value="Test briefing")
    def test_injects_briefing(self, mock_gen: MagicMock, project: Path, config: dict) -> None:
        """Successful regen injects briefing into CLAUDE.md."""
        from engram.server.briefing import regenerate_l0_briefing
        from engram.config import resolve_doc_paths

        target = project / "CLAUDE.md"
        target.write_text("# Project\n\nExisting content.\n")

        doc_paths = resolve_doc_paths(config, project)
        result = regenerate_l0_briefing(config, project, doc_paths)
        assert result is True
        text = target.read_text()
        assert "Test briefing" in text

    @patch("engram.server.briefing._generate_briefing", return_value=None)
    def test_returns_false_on_generation_failure(self, mock_gen: MagicMock, project: Path, config: dict) -> None:
        from engram.server.briefing import regenerate_l0_briefing
        from engram.config import resolve_doc_paths

        target = project / "CLAUDE.md"
        target.write_text("# Project\n")

        doc_paths = resolve_doc_paths(config, project)
        assert regenerate_l0_briefing(config, project, doc_paths) is False

    def test_build_lookup_patterns(self, project: Path, config: dict) -> None:
        from engram.config import resolve_doc_paths
        from engram.server.briefing import _build_lookup_patterns

        doc_paths = resolve_doc_paths(config, project)
        patterns = _build_lookup_patterns(doc_paths)
        assert patterns["concepts"].endswith("docs/decisions/concept_registry/current/C###.md")
        assert patterns["epistemic_current"].endswith("docs/decisions/epistemic_state/current/E###.md")
        assert patterns["epistemic_history"].endswith("docs/decisions/epistemic_state/history/E###.md")
        assert patterns["workflows"].endswith("docs/decisions/workflow_registry/current/W###.md")

    @patch("engram.server.briefing.subprocess.run")
    def test_generate_briefing_prompt_includes_lookup_hooks(self, mock_run: MagicMock, project: Path, config: dict) -> None:
        from engram.server.briefing import _generate_briefing

        mock_run.return_value = MagicMock(returncode=0, stdout="Briefing")
        lookup_patterns = {
            "concepts": "docs/decisions/concept_registry/current/C###.md",
            "epistemic_current": "docs/decisions/epistemic_state/current/E###.md",
            "epistemic_history": "docs/decisions/epistemic_state/history/E###.md",
            "workflows": "docs/decisions/workflow_registry/current/W###.md",
        }

        result = _generate_briefing(config, project, "### Timeline\nx", lookup_patterns)
        assert result == "Briefing"

        prompt = mock_run.call_args.args[0][-1]
        assert "Lookup Hooks (Use When Needed)" in prompt
        assert "short inline gloss" in prompt
        assert lookup_patterns["concepts"] in prompt
        assert lookup_patterns["epistemic_current"] in prompt
        assert lookup_patterns["epistemic_history"] in prompt
        assert lookup_patterns["workflows"] in prompt
