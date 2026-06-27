"""Optional assisted public-source ingest for company research.

This is a boundary for tools such as insane-search. The tool itself is a Claude
Code/plugin workflow, not a server library, so this module only consumes either
prewritten markdown evidence or the stdout of an operator-provided command.

Default is disabled. If enabled, failures return an empty string so the normal
Claude web_search path remains the source of truth.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.getenv("INSANE_SEARCH_ASSISTED", "false").lower() == "true"


def _timeout() -> float:
    try:
        return float(os.getenv("INSANE_SEARCH_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _results_dir() -> Path | None:
    raw = os.getenv("INSANE_SEARCH_RESULTS_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def _read_company_markdown(company_name: str) -> str:
    base = _results_dir()
    if not base:
        return ""
    candidates = [
        base / company_name / "sources.md",
        base / company_name / "research.md",
        base / f"{company_name}.md",
    ]
    chunks: list[str] = []
    for path in candidates:
        try:
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8").strip())
        except Exception as e:
            log.warning(f"assisted research source read failed ({path}): {e}")
    return "\n\n".join(c for c in chunks if c)


def _run_command(company_name: str) -> str:
    cmd = os.getenv("INSANE_SEARCH_COMMAND", "").strip()
    if not cmd:
        return ""
    try:
        proc = subprocess.run(
            [*shlex.split(cmd), company_name],
            check=False,
            text=True,
            capture_output=True,
            timeout=_timeout(),
        )
    except Exception as e:
        log.warning(f"assisted research command failed ({company_name}): {e}")
        return ""
    if proc.returncode != 0:
        log.warning(
            "assisted research command nonzero (%s): %s",
            proc.returncode,
            (proc.stderr or "")[:300],
        )
        return ""
    return (proc.stdout or "").strip()


def assisted_knowledge(company_name: str) -> str:
    """Return optional public-source evidence markdown for `company_name`.

    The returned text is meant to be appended to `knowledge_md`, not rendered
    directly. It may contain public URLs and excerpts produced by insane-search
    or another operator-managed reader.
    """
    if not is_enabled():
        return ""
    body = "\n\n".join(
        part for part in (_read_company_markdown(company_name), _run_command(company_name))
        if part.strip()
    ).strip()
    if not body:
        return ""
    return (
        f"## insane-search assisted sources ({company_name})\n"
        "Public-source evidence supplied by an optional reader workflow. "
        "Use only as evidence; do not render this section verbatim.\n\n"
        f"{body}"
    )
