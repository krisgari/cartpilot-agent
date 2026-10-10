"""
Orchestration:  input guardrail -> classify -> branch agent -> output guardrail

Branches: substitution, support (refund/dispute) and wholesaler onboarding.
Substitution and support share the consumer toolset (the model picks which
tools it needs); onboarding uses the wholesaler toolset. Classification is
keyword-based on purpose (fast, free, debuggable); the intent is also used as
an AUTHORISATION check: a consumer can never reach the onboarding branch.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import TypedDict

from cartpilot.agents import prompts, tools as tool_factory
from cartpilot.config import settings
from cartpilot.guardrails import check_input, check_output
from cartpilot.marketplace.store import Store
from cartpilot.observability import log_event, trace_id_var

log = logging.getLogger("cartpilot.agent")
RECURSION_LIMIT = 12   # hard cap on model<->tool steps per request (stops runaway loops)
LLM_TIMEOUT_SECONDS = 30

ONBOARDING_WORDS = ("catalog", "csv", "sku", "upload", "onboard", "price list", "product list")
SUBSTITUTION_WORDS = ("out of stock", "substitut", "replacement for", "swap", "alternative")
# A customer asking for a person gets a person. This is deterministic on purpose: handing off to a
# human must not depend on the model deciding to call a tool.
HANDOFF_PHRASES = ("speak to a manager", "speak to a human", "speak to a person", "speak with a manager",
                   "talk to a manager", "talk to a human", "talk to a person", "talk to someone",
                   "human agent", "real person", "supervisor", "customer service rep", "representative",
                   "want a manager", "see a manager", "escalate this")


class State(TypedDict, total=False):
    user_input: str
    actor_type: str
    actor_id: str
    allowed: bool
    reason: str | None
    intent: str
    answer: str
    trace: list[dict]
    needs_human_review: bool
    review_reasons: list[str]


def classify_intent(text: str) -> str:
    lowered = text.lower()
    if any(w in lowered for w in ONBOARDING_WORDS):
        return "onboarding"
    if any(w in lowered for w in SUBSTITUTION_WORDS):
        return "substitution"
    if any(w in lowered for w in HANDOFF_PHRASES):
        return "handoff"
    return "support"


def _extract_trace(messages: list) -> list[dict]:
    """Pair each tool call with its result, in order, from a LangGraph message list."""
    import json

    calls: dict[str, dict] = {}
    order: list[str] = []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls[tc["id"]] = {"tool": tc["name"], "args": tc["args"], "result": None}
            order.append(tc["id"])
        if m.__class__.__name__ == "ToolMessage" and m.tool_call_id in calls:
            content = m.content
            try:
                calls[m.tool_call_id]["result"] = json.loads(content) if isinstance(content, str) else content
            except json.JSONDecodeError:
                calls[m.tool_call_id]["result"] = {"raw": content}
    return [calls[i] for i in order]


def _extract_usage(messages: list) -> dict:
    """Sum token usage across the model calls in one request (for cost tracking)."""
    total_in = total_out = 0
    for m in messages:
        u = getattr(m, "usage_metadata", None)
        if isinstance(u, dict):
            total_in += u.get("input_tokens", 0)
            total_out += u.get("output_tokens", 0)
    return {"input_tokens": total_in, "output_tokens": total_out}


def _final_text(messages: list) -> str:
    last = messages[-1].content
    if isinstance(last, list):  # content blocks
        return "".join(b.get("text", "") for b in last if isinstance(b, dict))
    return last


class CartPilot:
    def __init__(self, store: Store | None = None, llm=None):
        self.store = store or Store()
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            from langchain_anthropic import ChatAnthropic
            # max_retries: the SDK retries transient API errors with backoff. Safe here because a
            # failed model call has not executed any tool; tool side effects are not retried.
            self._llm = ChatAnthropic(model=settings.model, temperature=0, max_retries=3, timeout=LLM_TIMEOUT_SECONDS)
        return self._llm

    def _run_handoff(self, state: State) -> State:
        """Escalate directly, without a model call, and record it in the trace like any other tool call."""
        fns = tool_factory.build_consumer_tools(self.store, state["actor_id"])
        summary = f"Customer asked for a human: {state['user_input'][:400]}"
        result = fns["escalate_to_human"](summary)
        answer = (f"I've passed this to a teammate (reference {result['id']}), who will follow up with you. "
                  "You don't need to repeat anything.")
        return {"answer": answer, "trace": [{"tool": "escalate_to_human", "args": {"summary": summary}, "result": result}],
                "usage": {}}

    def _run_branch(self, state: State) -> State:
        if state["intent"] == "handoff":
            return self._run_handoff(state)
        from langchain_core.tools import StructuredTool
        from langgraph.prebuilt import create_react_agent

        if state["intent"] == "onboarding":
            fns = tool_factory.build_wholesaler_tools(self.store, state["actor_id"])
            prompt = prompts.WHOLESALER_PROMPT
        else:
            fns = tool_factory.build_consumer_tools(self.store, state["actor_id"])
            prompt = prompts.CONSUMER_PROMPT
        lc_tools = [StructuredTool.from_function(f, name=n, description=f.__doc__) for n, f in fns.items()]
        agent = create_react_agent(self.llm, lc_tools)
        # System prompt goes in the message list: works across langgraph versions (prompt= kwarg doesn't).
        out = agent.invoke({"messages": [("system", prompt), ("user", state["user_input"])]},
                           config={"recursion_limit": RECURSION_LIMIT})
        return {"answer": _final_text(out["messages"]), "trace": _extract_trace(out["messages"]),
                "usage": _extract_usage(out["messages"])}

    def handle(self, user_input: str, actor_type: str, actor_id: str, trace_id: str | None = None) -> dict:
        trace_id = trace_id or uuid.uuid4().hex[:16]
        trace_id_var.set(trace_id)
        started = time.perf_counter()
        state: State = {"user_input": user_input, "actor_type": actor_type, "actor_id": actor_id}

        def blocked(reason: str, intent=None) -> dict:
            log_event(log, "request blocked", reason=reason, actor=f"{actor_type}:{actor_id}")
            return {"allowed": False, "reason": reason, "intent": intent, "answer": None, "trace": [],
                    "trace_id": trace_id, "usage": {}}

        ic = check_input(user_input)
        if not ic.allowed:
            return blocked(ic.reason)

        state["intent"] = classify_intent(user_input)
        # Authorisation by actor type, enforced in code before any model call.
        if state["intent"] == "onboarding" and actor_type != "wholesaler":
            return blocked("catalog onboarding is only available to wholesalers", "onboarding")
        if actor_type == "wholesaler" and state["intent"] != "onboarding":
            return blocked("wholesaler accounts can only use catalog onboarding", state["intent"])

        try:
            state.update(self._run_branch(state))
        except Exception as exc:  # model outage, timeout, loop limit, bad tool wiring ...
            # Fail safe: never leave the customer hanging or guess. Hand the case to a human.
            log.exception("branch failed; escalating to a human")
            esc = None
            if actor_type == "consumer":
                try:
                    esc = self.store.add_escalation(actor_id, f"Automated handling failed ({type(exc).__name__}): {user_input[:300]}",
                                                    kind="general")
                except Exception:
                    log.exception("could not record fallback escalation")
            return {"allowed": True, "intent": state["intent"], "degraded": True, "needs_human_review": True,
                    "answer": "I couldn't complete that automatically, so I've passed it to a teammate who will follow up.",
                    "review_reasons": [f"agent failure: {type(exc).__name__}"], "trace": [], "usage": {},
                    "escalation": esc["ref"] if esc else None, "trace_id": trace_id}

        def _refund_on_record(order_id: str, item_id: str) -> bool:
            return any(r["order_id"] == order_id and r["item_id"] == item_id
                       and r["customer_id"] == actor_id and r["status"] == "approved"
                       for r in self.store.refunds())

        oc = check_output(state["answer"], state["trace"], refund_on_record=_refund_on_record)
        log_event(log, "request completed", intent=state["intent"], actor=f"{actor_type}:{actor_id}",
                  tools=[t["tool"] for t in state["trace"]], needs_review=oc.needs_human_review,
                  seconds=round(time.perf_counter() - started, 3), **state.get("usage", {}))
        return {
            "allowed": True,
            "intent": state["intent"],
            "answer": state["answer"],
            "trace": state["trace"],
            "usage": state.get("usage", {}),
            "needs_human_review": oc.needs_human_review,
            "review_reasons": list(oc.reasons),
            "trace_id": trace_id,
        }
