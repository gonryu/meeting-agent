"""Optional Korean prose polish adapter with fidelity guard.

Designed for tools such as im-not-ai/humanize-korean. Default is disabled.
When enabled, a command receives the original text on stdin and returns polished
text on stdout. The polished output is accepted only if deterministic fidelity
checks pass.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_DATE_RE = re.compile(r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b")
_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:[,.]\d+)*(?:\s*(?:원|만원|억원|달러|%|개|건|명))?")


def is_enabled() -> bool:
    return os.getenv("KOREAN_POLISH_ENABLED", "false").lower() == "true"


def _max_change_ratio() -> float:
    try:
        return float(os.getenv("POLISH_MAX_CHANGE_RATIO", "0.30"))
    except ValueError:
        return 0.30


def _timeout() -> float:
    try:
        return float(os.getenv("KOREAN_POLISH_TIMEOUT", "45"))
    except ValueError:
        return 45.0


def _extract(rx: re.Pattern, text: str) -> list[str]:
    return rx.findall(text or "")


def _change_ratio(original: str, polished: str) -> float:
    if not original:
        return 0.0 if not polished else 1.0
    return abs(len(polished) - len(original)) / max(len(original), 1)


def validate_fidelity(
    original: str,
    polished: str,
    protected_terms: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Validate that polish did not alter protected facts or over-edit length."""
    reasons: list[str] = []
    if _extract(_URL_RE, original) != _extract(_URL_RE, polished):
        reasons.append("urls_changed")
    if _extract(_DATE_RE, original) != _extract(_DATE_RE, polished):
        reasons.append("dates_changed")
    if _extract(_NUMBER_RE, original) != _extract(_NUMBER_RE, polished):
        reasons.append("numbers_changed")
    for term in protected_terms or []:
        if term and (term in original) != (term in polished):
            reasons.append(f"protected_term_changed:{term}")
    if _change_ratio(original, polished) > _max_change_ratio():
        reasons.append("change_ratio_exceeded")
    return not reasons, reasons


def _run_command(text: str) -> str:
    cmd = os.getenv("KOREAN_POLISH_COMMAND", "").strip()
    if not cmd:
        return ""
    try:
        proc = subprocess.run(
            shlex.split(cmd),
            input=text,
            text=True,
            capture_output=True,
            timeout=_timeout(),
            check=False,
        )
    except Exception as e:
        log.warning(f"Korean polish command failed: {e}")
        return ""
    if proc.returncode != 0:
        log.warning("Korean polish command nonzero (%s): %s", proc.returncode, (proc.stderr or "")[:300])
        return ""
    return (proc.stdout or "").strip()


def polish_if_safe(
    text: str,
    *,
    protected_terms: list[str] | None = None,
) -> str:
    """Return polished text only when enabled and fidelity checks pass."""
    if not text or not is_enabled():
        return text
    polished = _run_command(text)
    if not polished or polished == text:
        return text
    ok, reasons = validate_fidelity(text, polished, protected_terms=protected_terms)
    if not ok:
        log.warning(f"Korean polish rejected by fidelity guard: {', '.join(reasons)}")
        return text
    return polished
