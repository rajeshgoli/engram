"""Compaction subsystem: graveyard moves and timeline collapse.

Moves DEAD/EVOLVED/refuted entries from living docs to append-only
graveyard files, leaving STUB pointers in the living doc. Also handles
timeline compaction and orphaned concept detection.
"""

from engram.compact.graveyard import (
    append_correction_block,
    compact_living_doc,
    find_orphaned_concepts,
    generate_stub,
    move_to_graveyard,
)
from engram.compact.timeline import compact_timeline

__all__ = [
    "append_correction_block",
    "compact_living_doc",
    "compact_timeline",
    "find_orphaned_concepts",
    "generate_stub",
    "move_to_graveyard",
]
