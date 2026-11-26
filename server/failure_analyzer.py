"""Failure diagnostics and Alpha recovery prompt builder for abrupt/empty agy turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .agy_runner import AgyResult
from .transcript import resolve_transcript_file


@dataclass(frozen=True)
class TurnDiagnostics:
    category: str  # permission_denied | auth_required | quota_or_rate_limit | timeout | tool_error | empty_response
    summary: str
    attempted_tools: list[str] = field(default_factory=list)
    error_details: list[str] = field(default_factory=list)
    failed_step_index: int | None = None
    recoverable_via_agent: bool = True


_PERM_PATTERNS = (
    "permission check failed",
    "user denied permission",
    "requires approval",
    "soft-denied",
    "not allowed",
)
_AUTH_PATTERNS = (
    "authentication required",
    "not logged into antigravity",
    "unauthenticated",
    "invalid_grant",
    "oauth",
)
_QUOTA_PATTERNS = (
    "quota",
    "rate limit",
    "resource_exhausted",
    "429",
    "too many requests",
)
_TIMEOUT_PATTERNS = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "print-timeout",
)


def _extract_turn_evidence(
    transcript_path: Path | None,
    after_step_index: int,
) -> tuple[list[str], list[str], int | None]:
    """Read tool_calls and ERROR steps appended after `after_step_index`."""
    attempted_tools: list[str] = []
    error_details: list[str] = []
    failed_step_index: int | None = None

    if transcript_path is None or not transcript_path.exists():
        return attempted_tools, error_details, failed_step_index

    with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            idx = obj.get("step_index")
            if not isinstance(idx, int) or idx <= after_step_index:
                continue

            tool_calls = obj.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tname = str(tc.get("name") or "unknown_tool")
                    args = tc.get("args") or {}
                    detail = ""
                    if isinstance(args, dict):
                        for key in ("CommandLine", "toolSummary", "Path", "TargetFile", "Url", "query"):
                            val = args.get(key)
                            if val:
                                detail = str(val).strip().strip('"')
                                break
                    if detail:
                        if len(detail) > 160:
                            detail = detail[:157] + "..."
                        attempted_tools.append(f"{tname}({detail})")
                    else:
                        attempted_tools.append(tname)

            if obj.get("status") == "ERROR" or obj.get("error"):
                failed_step_index = idx
                err = str(obj.get("error") or obj.get("content") or "").strip()
                if err:
                    # Clean redundant wrapper lines while keeping the concrete reason
                    err = re.sub(r"\s+", " ", err)
                    error_details.append(err[:600])

    return attempted_tools, error_details, failed_step_index


def analyze_turn_failure(
    result: AgyResult,
    *,
    brain_dir: Path,
    conversation_id: str | None,
    pre_turn_step_index: int,
) -> TurnDiagnostics:
    """Inspect AgyResult + transcript evidence to classify and explain a failed/empty turn."""
    tpath = (
        resolve_transcript_file(brain_dir=brain_dir, conversation_id=conversation_id)
        if conversation_id
        else None
    )
    attempted_tools, error_details, failed_step = _extract_turn_evidence(
        tpath, pre_turn_step_index
    )

    if result.error:
        error_details.append(result.error[:500])
    if result.stderr:
        # Include stderr only if informative and not already duplicated
        stderr_clean = re.sub(r"\s+", " ", result.stderr).strip()
        if stderr_clean and not any(stderr_clean[:80] in e for e in error_details):
            error_details.append(stderr_clean[:500])

    combined_lower = " ".join(error_details).lower()

    if any(p in combined_lower for p in _PERM_PATTERNS):
        return TurnDiagnostics(
            category="permission_denied",
            summary="A tool or shell command required interactive approval, which headless mode cannot grant.",
            attempted_tools=attempted_tools,
            error_details=error_details,
            failed_step_index=failed_step,
            recoverable_via_agent=True,
        )
    if any(p in combined_lower for p in _AUTH_PATTERNS):
        return TurnDiagnostics(
            category="auth_required",
            summary="Antigravity CLI authentication is missing or expired on the server.",
            attempted_tools=attempted_tools,
            error_details=error_details,
            failed_step_index=failed_step,
            recoverable_via_agent=False,
        )
    if any(p in combined_lower for p in _QUOTA_PATTERNS):
        return TurnDiagnostics(
            category="quota_or_rate_limit",
            summary="Model quota or rate limit was reached.",
            attempted_tools=attempted_tools,
            error_details=error_details,
            failed_step_index=failed_step,
            recoverable_via_agent=False,
        )
    if any(p in combined_lower for p in _TIMEOUT_PATTERNS):
        return TurnDiagnostics(
            category="timeout",
            summary="The agent turn timed out before completing.",
            attempted_tools=attempted_tools,
            error_details=error_details,
            failed_step_index=failed_step,
            recoverable_via_agent=True,
        )
    if error_details or result.status != "SUCCESS":
        return TurnDiagnostics(
            category="tool_error",
            summary=f"The agent turn ended abnormally (status={result.status}).",
            attempted_tools=attempted_tools,
            error_details=error_details,
            failed_step_index=failed_step,
            recoverable_via_agent=True,
        )
    return TurnDiagnostics(
        category="empty_response",
        summary="The agent completed without producing a text reply.",
        attempted_tools=attempted_tools,
        error_details=error_details,
        failed_step_index=failed_step,
        recoverable_via_agent=True,
    )


def build_recovery_prompt(
    user_message: str,
    diag: TurnDiagnostics,
    *,
    tz_label: str = "PST",
) -> str:
    """Build a strict no-tool prompt asking Alpha to explain the failure in-character."""
    tools_str = "; ".join(diag.attempted_tools[:4]) if diag.attempted_tools else "(none)"
    err_str = " | ".join(diag.error_details[:2]) if diag.error_details else "(no error text)"
    return (
        f"[channel: bridge-recovery | category: {diag.category} | tz: {tz_label}]\n"
        "SYSTEM RECOVERY INSTRUCTION (do NOT call any tools or run any commands on this turn):\n"
        "Your previous turn terminated before returning a text reply to the user.\n"
        f"- User's request: {user_message[:400]}\n"
        f"- Failure category: {diag.category}\n"
        f"- Diagnostic summary: {diag.summary}\n"
        f"- Attempted tool(s): {tools_str}\n"
        f"- Underlying error: {err_str[:700]}\n\n"
        "Reply directly to the user in your normal Alpha voice, matching the language of the user's request above. "
        "Plainly explain what you tried to do, why it was blocked or failed, and what concrete options the user has "
        "(for example, adjusting CLI permissions, using a built-in command like /status, or handling it over SSH). "
        "Do not fabricate results and do not invoke any tools."
    )


def format_fallback_report(diag: TurnDiagnostics, conversation_id: str | None = None) -> str:
    """Deterministic human-readable report used when Alpha recovery is skipped or fails."""
    lines = [
        f"Turn could not complete ({diag.category}).",
        f"Reason: {diag.summary}",
    ]
    if diag.attempted_tools:
        lines.append("Attempted: " + "; ".join(diag.attempted_tools[:3]))
    if diag.error_details:
        lines.append("Detail: " + diag.error_details[0][:400])
    if diag.category == "auth_required":
        lines.append("Fix: SSH into the server (`ssh agy-staging`) and run `agy` to re-authenticate.")
    elif diag.category == "permission_denied":
        lines.append("Fix: Allow the tool in `~/.gemini/antigravity-cli/settings.json` or handle it via `alpha-ssh`.")
    if conversation_id:
        step_info = f" step={diag.failed_step_index}" if diag.failed_step_index is not None else ""
        lines.append(f"[conv={conversation_id[:8]}{step_info}]")
    return "\n".join(lines)
