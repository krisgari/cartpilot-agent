"""
Guardrails.

Input:  block obvious prompt-injection / policy-override attempts.
Output: verify the answer's *claims* against what tools actually did. If the
        model says "I've refunded you" but no approved refund exists in the
        tool trace, flag it for human review instead of letting it through.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_INPUT_CHARS = 4000

_INJECTION = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"disregard (the |your )?(system|policy|rules)",
    r"you are now",
    r"(reveal|show|print) (your )?(system )?prompt",
    r"override (the )?(refund )?(policy|limit)",
    r"approve (this|the) refund (regardless|anyway)",
]


@dataclass(frozen=True)
class InputCheck:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class OutputCheck:
    needs_human_review: bool
    reasons: tuple[str, ...] = ()


def check_input(text: str) -> InputCheck:
    if not text or not text.strip():
        return InputCheck(False, "empty request")
    if len(text) > MAX_INPUT_CHARS:
        return InputCheck(False, "request too long")
    lowered = text.lower()
    for pattern in _INJECTION:
        if re.search(pattern, lowered):
            return InputCheck(False, "request looks like a prompt-injection attempt")
    return InputCheck(True)


_REFUND_CLAIM = re.compile(r"\b(refunded|refund (has been|was|is)( already)? (issued|processed|approved)|issued (a|your) refund)\b", re.I)
_SUB_CLAIM = re.compile(r"\b(substituted|swapped|replaced|substitution (has been|was) (applied|made))\b", re.I)


def _succeeded(trace: list[dict], tool: str, status: str | None = None) -> bool:
    for step in trace:
        if step["tool"] != tool:
            continue
        result = step.get("result")
        if isinstance(result, dict):
            if status is None and not result.get("error"):
                return True
            if status is not None and result.get("status") == status:
                return True
    return False


def _refund_already_on_record(trace: list[dict], refund_on_record) -> bool:
    """True if the model asked about an item whose refund the database confirms already exists.

    This covers honest follow-ups ("that refund was already issued") without trusting the model's words:
    the claim must be about an order/item the tool call named, and the store must hold an approved refund for it.
    """
    if refund_on_record is None:
        return False
    for step in trace:
        if step["tool"] != "request_refund":
            continue
        args = step.get("args") or {}
        if args.get("order_id") and args.get("item_id") and refund_on_record(args["order_id"], args["item_id"]):
            return True
    return False


def check_output(answer: str, trace: list[dict], *, refund_on_record=None) -> OutputCheck:
    reasons: list[str] = []
    if (_REFUND_CLAIM.search(answer)
            and not _succeeded(trace, "request_refund", "approved")
            and not _refund_already_on_record(trace, refund_on_record)):
        reasons.append("answer claims a refund was issued but no approved refund exists in the tool trace")
    if _SUB_CLAIM.search(answer) and not _succeeded(trace, "apply_substitution"):
        reasons.append("answer claims a substitution was applied but apply_substitution did not succeed")
    return OutputCheck(bool(reasons), tuple(reasons))
