import json
import logging

from cartpilot.agents.router import _extract_usage
from cartpilot.observability import JsonFormatter, Metrics, log_event, record_request, trace_id_var, metrics


class _M:
    def __init__(self, usage=None):
        if usage is not None:
            self.usage_metadata = usage


def test_usage_summed_across_model_calls_and_ignores_others():
    msgs = [_M({"input_tokens": 100, "output_tokens": 20}), _M(), _M({"input_tokens": 150, "output_tokens": 30})]
    assert _extract_usage(msgs) == {"input_tokens": 250, "output_tokens": 50}


def test_prometheus_render_counters_and_histogram():
    m = Metrics()
    m.inc("reqs", intent="support", outcome="ok")
    m.inc("reqs", intent="support", outcome="ok")
    m.observe("secs", 0.7)
    m.observe("secs", 40)
    out = m.render()
    assert 'reqs{intent="support",outcome="ok"} 2' in out
    assert 'secs_bucket{le="1"} 1' in out and 'secs_bucket{le="+Inf"} 2' in out and "secs_count 2" in out


def test_record_request_outcomes_and_tool_status():
    before = metrics.render()
    record_request({"allowed": True, "intent": "support", "usage": {"input_tokens": 10, "output_tokens": 5},
                    "trace": [{"tool": "request_refund", "result": {"status": "approved"}},
                              {"tool": "get_my_order", "result": {"error": "x"}}]}, 1.2)
    record_request({"allowed": False, "intent": None}, 0.01)
    out = metrics.render()
    assert 'cartpilot_requests_total{intent="support",outcome="ok"}' in out
    assert 'cartpilot_requests_total{intent="none",outcome="blocked"}' in out
    assert 'cartpilot_tool_calls_total{status="approved",tool="request_refund"}' in out
    assert 'cartpilot_tool_calls_total{status="error",tool="get_my_order"}' in out
    assert out != before


def test_json_logs_carry_trace_id_and_fields():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    rec.fields = {"tool": "request_refund"}
    trace_id_var.set("abc123")
    line = json.loads(JsonFormatter().format(rec))
    assert line["trace_id"] == "abc123" and line["tool"] == "request_refund" and line["msg"] == "hello"
