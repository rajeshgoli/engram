"""Knowledge server: watch → accumulate → dispatch → validate.

Entry point: :func:`run_server` starts the foreground server loop.
:func:`get_status` returns current server state for CLI display.
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Any

from engram.config import resolve_doc_paths
from engram.fold.chunker import queue_is_empty
from engram.server.briefing import regenerate_l0_briefing
from engram.server.buffer import ContextBuffer
from engram.server.db import ServerDB
from engram.server.dispatcher import Dispatcher
from engram.server.watcher import FileWatcher, GitPoller, SessionPoller

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60  # seconds


def run_server(config: dict[str, Any], project_root: Path) -> None:
    """Run the engram knowledge server in the foreground.

    The server loop:
    1. Initialises watchers (filesystem, git, sessions)
    2. Recovers any incomplete dispatches from a previous crash
    3. Polls on a configurable interval
    4. Dispatches when buffer is full or drift threshold exceeded

    Parameters
    ----------
    config:
        Engram config dict.
    project_root:
        Project root directory.
    """
    db_path = project_root / ".engram" / "engram.db"
    db = ServerDB(db_path)
    buffer = ContextBuffer(config, project_root, db)
    dispatcher = Dispatcher(config, project_root, db)

    poll_interval = config.get("poll_interval", DEFAULT_POLL_INTERVAL)

    # --- Crash recovery ---
    log.info("Checking for incomplete dispatches...")
    stale = db.recover_on_startup()
    for d in stale:
        log.info("Recovering dispatch %d (state=%s)", d["id"], d["state"])
        dispatcher.recover_dispatch(d)

    # --- Startup L0 check (quiet restart with stale flag) ---
    if db.is_l0_stale() and queue_is_empty(project_root):
        log.info("Startup: regenerating stale L0 briefing...")
        doc_paths = resolve_doc_paths(config, project_root)
        if regenerate_l0_briefing(config, project_root, doc_paths):
            db.clear_l0_stale()

    # --- Callback for watchers ---
    def on_change(
        path: str,
        item_type: str,
        chars: int,
        date: str | None,
        metadata: str | None,
    ) -> None:
        buffer.add_item(path, item_type, chars, date, metadata)

    # --- Start watchers ---
    file_watcher = FileWatcher(config, project_root, on_change)
    file_watcher.start()

    source_dirs = config.get("sources", {}).get("docs", [])
    git_poller = GitPoller(project_root, on_change, source_dirs)
    session_poller = SessionPoller(config, on_change)

    # Restore polling bookmarks from DB
    state = db.get_server_state()
    if state.get("last_poll_commit"):
        git_poller.set_last_commit(state["last_poll_commit"])
    if state.get("last_session_mtime"):
        session_poller.set_last_mtime(state["last_session_mtime"])

    # --- Signal handling ---
    running = True

    def _shutdown(signum: int, frame: object) -> None:
        nonlocal running
        log.info("Received signal %d, shutting down...", signum)
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "Engram server started (poll_interval=%ds, project=%s)",
        poll_interval, project_root,
    )

    # --- Main loop ---
    while running:
        try:
            # Poll git
            new_commits = git_poller.poll()
            if new_commits:
                log.info("Git: %d new commit(s)", len(new_commits))
                db.update_server_state(
                    last_poll_commit=git_poller.get_last_commit(),
                )

            # Poll sessions
            new_sessions = session_poller.poll()
            if new_sessions:
                log.info("Sessions: %d new entry/entries", new_sessions)
                mtime = session_poller.get_last_mtime()
                if mtime is not None:
                    db.update_server_state(last_session_mtime=mtime)

            # Check dispatch readiness
            reason = buffer.should_dispatch()
            if reason:
                log.info("Dispatch triggered: %s", reason)
                success = dispatcher.dispatch()
                if success:
                    log.info("Dispatch completed successfully")
                else:
                    log.warning("Dispatch failed")

            # Unconditional L0 check — runs every iteration, not nested
            if db.is_l0_stale() and queue_is_empty(project_root):
                log.info("Queue drained: regenerating L0 briefing...")
                doc_paths = resolve_doc_paths(config, project_root)
                if regenerate_l0_briefing(config, project_root, doc_paths):
                    db.clear_l0_stale()
                # If regen fails, l0_stale stays set — next iteration retries

            # Update poll time
            db.update_server_state(
                last_poll_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )

        except Exception:
            log.exception("Error in server loop")

        # Sleep in small increments to allow clean shutdown
        for _ in range(poll_interval):
            if not running:
                break
            time.sleep(1)

    # --- Cleanup ---
    file_watcher.stop()
    log.info("Engram server stopped")


def get_status(config: dict[str, Any], project_root: Path) -> dict[str, Any]:
    """Get current server status for CLI display.

    Returns a dict with buffer, dispatch, and server state info.
    """
    db_path = project_root / ".engram" / "engram.db"

    if not db_path.exists():
        return {
            "running": False,
            "error": "No database found. Run 'engram init' first.",
        }

    db = ServerDB(db_path)
    buffer = ContextBuffer(config, project_root, db)

    server_state = db.get_server_state()
    fill_info = buffer.get_fill_info()
    last_dispatch = db.get_last_dispatch()
    recent_dispatches = db.get_recent_dispatches(limit=5)
    pending_items = db.get_buffer_items()

    return {
        "buffer": fill_info,
        "pending_items": len(pending_items),
        "last_dispatch": last_dispatch,
        "recent_dispatches": recent_dispatches,
        "server_state": server_state,
    }
