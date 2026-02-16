"""Shared dispatch utilities for fold agent invocation.

Used by both :mod:`engram.server.dispatcher` and :mod:`engram.bootstrap.fold`
to avoid duplicating agent invocation and doc-reading logic.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def invoke_agent(
    config: dict[str, Any],
    project_root: Path,
    prompt: str,
    timeout: int = 600,
) -> bool:
    """Shell out to the configured fold agent CLI.

    Builds the command from *config* (``agent_command`` or ``model``),
    appends *prompt* as the final argument, and runs with *timeout*.

    Returns True if the agent completed successfully (rc=0).
    """
    model = config.get("model", "sonnet")
    agent_cmd = config.get("agent_command")
    if agent_cmd:
        cmd = agent_cmd.split()
    else:
        cmd = ["claude", "--print", "--model", model]

    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=timeout,
        )
        if result.returncode != 0:
            log.error("Fold agent failed (rc=%d): %s", result.returncode, result.stderr[:500])
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("Fold agent timed out (%d s)", timeout)
        return False
    except FileNotFoundError:
        log.error("Agent command not found: %s", cmd[0])
        return False


def read_docs(doc_paths: dict[str, Path], keys: tuple[str, ...]) -> dict[str, str]:
    """Read document contents for the given keys.

    Returns a dict mapping each key to its file contents (empty string
    if the file doesn't exist).
    """
    contents: dict[str, str] = {}
    for key in keys:
        p = doc_paths.get(key)
        if p and p.exists():
            contents[key] = p.read_text()
        else:
            contents[key] = ""
    return contents
