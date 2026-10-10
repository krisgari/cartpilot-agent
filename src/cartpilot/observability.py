"""
Structured JSON logs, per-request trace ids, and Prometheus-format metrics. Standard library only.

Every log line is one JSON object with the request's trace_id, so one query in your log tool
reconstructs a whole request. For distributed tracing / LLM-level tracing, LangSmith works with no
code change: set LANGSMITH_TRACING=true and LANGSMITH_API_KEY (langchain/langgraph auto-instrument).
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
from collections import defaultdict

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
                 "logger": record.name, "msg": record.getMessage(), "trace_id": trace_id_var.get()}
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(level)


def log_event(logger: logging.Logger, msg: str, **fields) -> None:
    logger.info(msg, extra={"fields": fields})


_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60)


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self.hist: dict[str, dict] = {}

    def inc(self, name: str, amount: float = 1, **labels) -> None:
        with self._lock:
            self.counters[(name, tuple(sorted(labels.items())))] += amount

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            h = self.hist.setdefault(name, {"buckets": [0] * len(_BUCKETS), "sum": 0.0, "count": 0})
            for i, b in enumerate(_BUCKETS):
                if value <= b:
                    h["buckets"][i] += 1
            h["sum"] += value
            h["count"] += 1

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), v in sorted(self.counters.items()):
                lbl = ",".join(f'{k}="{val}"' for k, val in labels)
                lines.append(f"{name}{{{lbl}}} {v:g}" if lbl else f"{name} {v:g}")
            for name, h in sorted(self.hist.items()):
                for b, c in zip(_BUCKETS, h["buckets"]):
                    lines.append(f'{name}_bucket{{le="{b}"}} {c}')
                lines.append(f'{name}_bucket{{le="+Inf"}} {h["count"]}')
                lines.append(f"{name}_sum {h['sum']:.4f}")
                lines.append(f"{name}_count {h['count']}")
        return "\n".join(lines) + "\n"


metrics = Metrics()


def record_request(result: dict, seconds: float) -> None:
    """Update metrics from a finished agent result (called by the API layer)."""
    outcome = "blocked" if not result.get("allowed") else ("degraded" if result.get("degraded") else
              ("needs_review" if result.get("needs_human_review") else "ok"))
    metrics.inc("cartpilot_requests_total", intent=result.get("intent") or "none", outcome=outcome)
    metrics.observe("cartpilot_request_seconds", seconds)
    usage = result.get("usage") or {}
    metrics.inc("cartpilot_tokens_total", usage.get("input_tokens", 0), kind="input")
    metrics.inc("cartpilot_tokens_total", usage.get("output_tokens", 0), kind="output")
    for step in result.get("trace") or []:
        res = step.get("result")
        status = res.get("status", "error" if "error" in res else "ok") if isinstance(res, dict) else "ok"
        metrics.inc("cartpilot_tool_calls_total", tool=step["tool"], status=status)
