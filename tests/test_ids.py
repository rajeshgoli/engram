"""Tests for engram.fold.ids — stable ID allocation."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from engram.fold.ids import (
    CATEGORIES,
    IDAllocator,
    IDAllocatorError,
    estimate_new_entities,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a fresh database path inside tmp_path."""
    return tmp_path / ".engram" / "engram.db"


@pytest.fixture
def allocator(db_path: Path) -> IDAllocator:
    """Return a fresh IDAllocator."""
    with IDAllocator(db_path) as alloc:
        yield alloc


# ------------------------------------------------------------------
# Basic allocation
# ------------------------------------------------------------------

class TestSingleAllocation:
    def test_first_ids_start_at_001(self, allocator: IDAllocator) -> None:
        assert allocator.next_id("C") == "C001"
        assert allocator.next_id("E") == "E001"
        assert allocator.next_id("W") == "W001"

    def test_sequential_ids(self, allocator: IDAllocator) -> None:
        ids = [allocator.next_id("C") for _ in range(5)]
        assert ids == ["C001", "C002", "C003", "C004", "C005"]

    def test_categories_are_independent(self, allocator: IDAllocator) -> None:
        allocator.next_id("C")  # C001
        allocator.next_id("C")  # C002
        # E should still start at 1
        assert allocator.next_id("E") == "E001"

    def test_invalid_category_raises(self, allocator: IDAllocator) -> None:
        with pytest.raises(IDAllocatorError, match="Invalid category"):
            allocator.next_id("X")

    def test_zero_count_raises(self, allocator: IDAllocator) -> None:
        with pytest.raises(IDAllocatorError, match="count must be >= 1"):
            allocator.reserve_range("C", 0)

    def test_negative_count_raises(self, allocator: IDAllocator) -> None:
        with pytest.raises(IDAllocatorError, match="count must be >= 1"):
            allocator.reserve_range("C", -1)


# ------------------------------------------------------------------
# Range reservation
# ------------------------------------------------------------------

class TestRangeReservation:
    def test_reserve_range(self, allocator: IDAllocator) -> None:
        ids = allocator.reserve_range("C", 5)
        assert ids == ["C001", "C002", "C003", "C004", "C005"]

    def test_subsequent_range_continues(self, allocator: IDAllocator) -> None:
        allocator.reserve_range("E", 3)  # E001-E003
        ids = allocator.reserve_range("E", 2)
        assert ids == ["E004", "E005"]

    def test_single_range_then_single(self, allocator: IDAllocator) -> None:
        allocator.reserve_range("W", 10)  # W001-W010
        assert allocator.next_id("W") == "W011"

    def test_ranges_are_disjoint_across_calls(self, allocator: IDAllocator) -> None:
        r1 = allocator.reserve_range("C", 3)  # C001-C003
        r2 = allocator.reserve_range("C", 3)  # C004-C006
        assert set(r1).isdisjoint(set(r2))


# ------------------------------------------------------------------
# IDs never reused
# ------------------------------------------------------------------

class TestNoReuse:
    def test_ids_never_reuse_after_gap(self, db_path: Path) -> None:
        """Simulate 'deletion' by closing and reopening — counter persists."""
        alloc1 = IDAllocator(db_path)
        alloc1.reserve_range("C", 5)  # C001-C005
        alloc1.close()

        # Reopen — counter should continue from 6
        alloc2 = IDAllocator(db_path)
        assert alloc2.next_id("C") == "C006"
        alloc2.close()

    def test_counter_survives_reopen(self, db_path: Path) -> None:
        alloc = IDAllocator(db_path)
        alloc.reserve_range("E", 20)
        alloc.close()

        alloc2 = IDAllocator(db_path)
        assert alloc2.peek("E") == 21
        alloc2.close()


# ------------------------------------------------------------------
# Peek
# ------------------------------------------------------------------

class TestPeek:
    def test_peek_initial(self, allocator: IDAllocator) -> None:
        assert allocator.peek("C") == 1

    def test_peek_after_allocation(self, allocator: IDAllocator) -> None:
        allocator.reserve_range("C", 5)
        assert allocator.peek("C") == 6

    def test_peek_does_not_advance(self, allocator: IDAllocator) -> None:
        allocator.peek("C")
        allocator.peek("C")
        assert allocator.next_id("C") == "C001"

    def test_peek_all(self, allocator: IDAllocator) -> None:
        allocator.reserve_range("C", 3)
        allocator.reserve_range("E", 1)
        state = allocator.peek_all()
        assert state == {"C": 4, "E": 2, "W": 1}


# ------------------------------------------------------------------
# Chunk pre-assignment
# ------------------------------------------------------------------

class TestPreAssign:
    def test_pre_assign_all_categories(self, allocator: IDAllocator) -> None:
        result = allocator.pre_assign_for_chunk(
            new_concepts=2, new_epistemic=1, new_workflows=3
        )
        assert result["C"] == ["C001", "C002"]
        assert result["E"] == ["E001"]
        assert result["W"] == ["W001", "W002", "W003"]

    def test_pre_assign_zero_omitted(self, allocator: IDAllocator) -> None:
        result = allocator.pre_assign_for_chunk(new_concepts=1)
        assert "C" in result
        assert "E" not in result
        assert "W" not in result

    def test_pre_assign_advances_counter(self, allocator: IDAllocator) -> None:
        allocator.pre_assign_for_chunk(new_concepts=5)
        assert allocator.peek("C") == 6

    def test_successive_chunks_disjoint(self, allocator: IDAllocator) -> None:
        r1 = allocator.pre_assign_for_chunk(new_concepts=3)
        r2 = allocator.pre_assign_for_chunk(new_concepts=3)
        assert set(r1["C"]).isdisjoint(set(r2["C"]))
        assert r1["C"] == ["C001", "C002", "C003"]
        assert r2["C"] == ["C004", "C005", "C006"]

    def test_pre_assign_respects_min_next_ids(self, allocator: IDAllocator) -> None:
        r1 = allocator.pre_assign_for_chunk(new_concepts=2, min_next_ids={"C": 42})
        assert r1["C"] == ["C042", "C043"]
        assert allocator.peek("C") == 44

    def test_pre_assign_min_next_ids_does_not_move_backwards(self, allocator: IDAllocator) -> None:
        allocator.pre_assign_for_chunk(new_concepts=3)  # C001-C003, next=4
        r2 = allocator.pre_assign_for_chunk(new_concepts=1, min_next_ids={"C": 2})
        assert r2["C"] == ["C004"]
        assert allocator.peek("C") == 5


# ------------------------------------------------------------------
# Concurrent safety
# ------------------------------------------------------------------

class TestConcurrentSafety:
    def test_concurrent_allocations_no_duplicates(self, db_path: Path) -> None:
        """Multiple threads reserving ranges should produce disjoint IDs."""
        # Pre-create the DB so all threads hit the same file
        init_alloc = IDAllocator(db_path)
        init_alloc.close()

        results: list[list[str]] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def reserve_worker(n: int) -> None:
            try:
                alloc = IDAllocator(db_path)
                ids = alloc.reserve_range("C", n)
                alloc.close()
                with lock:
                    results.append(ids)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=reserve_worker, args=(5,)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors in threads: {errors}"

        # All IDs should be unique
        all_ids = [id_ for batch in results for id_ in batch]
        assert len(all_ids) == 50
        assert len(set(all_ids)) == 50, f"Duplicate IDs found: {len(all_ids) - len(set(all_ids))}"

    def test_concurrent_mixed_categories(self, db_path: Path) -> None:
        """Concurrent allocation across different categories."""
        init_alloc = IDAllocator(db_path)
        init_alloc.close()

        results: dict[str, list[list[str]]] = {"C": [], "E": [], "W": []}
        lock = threading.Lock()
        errors: list[Exception] = []

        def reserve_worker(cat: str, n: int) -> None:
            try:
                alloc = IDAllocator(db_path)
                ids = alloc.reserve_range(cat, n)
                alloc.close()
                with lock:
                    results[cat].append(ids)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = []
        for cat in ("C", "E", "W"):
            for _ in range(5):
                threads.append(threading.Thread(target=reserve_worker, args=(cat, 3)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors in threads: {errors}"

        # Each category should have 15 unique IDs
        for cat in ("C", "E", "W"):
            all_ids = [id_ for batch in results[cat] for id_ in batch]
            assert len(all_ids) == 15
            assert len(set(all_ids)) == 15


# ------------------------------------------------------------------
# Entity estimation
# ------------------------------------------------------------------

class TestEstimateNewEntities:
    def test_empty_items(self) -> None:
        assert estimate_new_entities([]) == {"C": 0, "E": 0, "W": 0}

    def test_items_without_hints(self) -> None:
        items = [{"type": "doc"}, {"type": "issue"}]
        assert estimate_new_entities(items) == {"C": 0, "E": 0, "W": 0}

    def test_items_with_hints(self) -> None:
        items = [
            {"type": "issue", "entity_hints": [{"category": "C"}, {"category": "E"}]},
            {"type": "doc", "entity_hints": [{"category": "W"}]},
            {"type": "session"},
        ]
        assert estimate_new_entities(items) == {"C": 1, "E": 1, "W": 1}

    def test_multiple_hints_same_category(self) -> None:
        items = [
            {"type": "issue", "entity_hints": [{"category": "C"}, {"category": "C"}]},
        ]
        assert estimate_new_entities(items) == {"C": 2, "E": 0, "W": 0}

    def test_invalid_category_in_hint_ignored(self) -> None:
        items = [
            {"type": "doc", "entity_hints": [{"category": "X"}]},
        ]
        assert estimate_new_entities(items) == {"C": 0, "E": 0, "W": 0}


# ------------------------------------------------------------------
# Database initialization
# ------------------------------------------------------------------

class TestDatabaseInit:
    def test_creates_db_directory(self, tmp_path: Path) -> None:
        db_path = tmp_path / "deep" / "nested" / "engram.db"
        alloc = IDAllocator(db_path)
        assert db_path.parent.exists()
        alloc.close()

    def test_table_exists_after_init(self, db_path: Path) -> None:
        alloc = IDAllocator(db_path)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT category, next_id FROM id_counters ORDER BY category"
        ).fetchall()
        conn.close()
        assert rows == [("C", 1), ("E", 1), ("W", 1)]
        alloc.close()

    def test_reinit_does_not_reset_counters(self, db_path: Path) -> None:
        alloc = IDAllocator(db_path)
        alloc.reserve_range("C", 10)
        alloc.close()

        # Re-init — counter should not reset
        alloc2 = IDAllocator(db_path)
        assert alloc2.peek("C") == 11
        alloc2.close()


# ------------------------------------------------------------------
# Context manager
# ------------------------------------------------------------------

class TestContextManager:
    def test_with_statement(self, db_path: Path) -> None:
        with IDAllocator(db_path) as alloc:
            assert alloc.next_id("C") == "C001"
        # After exit, allocator still works for peek (per-call connections)
        alloc2 = IDAllocator(db_path)
        assert alloc2.peek("C") == 2

    def test_close_is_idempotent(self, db_path: Path) -> None:
        alloc = IDAllocator(db_path)
        alloc.close()
        alloc.close()  # Should not raise


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------

class TestIDFormatting:
    def test_three_digit_padding(self, allocator: IDAllocator) -> None:
        assert allocator.next_id("C") == "C001"

    def test_no_extra_padding_over_999(self, db_path: Path) -> None:
        """IDs above 999 just use more digits — no truncation."""
        alloc = IDAllocator(db_path)
        # Fast-forward counter
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE id_counters SET next_id = 999 WHERE category = 'C'")
        conn.commit()
        conn.close()

        assert alloc.next_id("C") == "C999"
        assert alloc.next_id("C") == "C1000"
        alloc.close()
