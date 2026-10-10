"""
FastAPI service.   Run:  cd src && uvicorn cartpilot.api.app:app --reload

Identity comes from a verified JWT (see cartpilot/security.py); the body never carries identity.
  consumer / wholesaler -> POST /query
  ops                   -> GET /ops/escalations, POST /ops/escalations/{id}/resolve
Fail-closed: with no CARTPILOT_JWT_SECRET (and CARTPILOT_ENV != dev) every protected route returns 503.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from cartpilot.agents.router import CartPilot
from cartpilot.observability import configure_logging, log_event, metrics, record_request, trace_id_var
from cartpilot.security import Actor, AuthError, resolve_actor

configure_logging()
log = logging.getLogger("cartpilot.api")
app = FastAPI(title="CartPilot", version="0.2.0")
_agent: CartPilot | None = None


def get_agent() -> CartPilot:
    global _agent
    if _agent is None:
        _agent = CartPilot()
    return _agent


# --- identity -----------------------------------------------------------------------------
def current_actor(authorization: str | None = Header(default=None),
                  x_dev_actor: str | None = Header(default=None)) -> Actor:
    try:
        return resolve_actor(authorization, x_dev_actor)
    except AuthError as exc:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=headers)


def require_role(*roles: str):
    def dep(actor: Actor = Depends(current_actor)) -> Actor:
        if actor.role not in roles:
            raise HTTPException(status_code=403, detail=f"role '{actor.role}' cannot use this endpoint")
        return actor
    return dep


# --- request context ----------------------------------------------------------------------
@app.middleware("http")
async def request_context(request: Request, call_next):
    tid = (request.headers.get("x-trace-id") or uuid.uuid4().hex)[:32]
    trace_id_var.set(tid)
    request.state.trace_id = tid
    started = time.perf_counter()
    response: Response = await call_next(request)
    response.headers["X-Trace-Id"] = tid
    metrics.inc("cartpilot_http_requests_total", path=request.url.path, status=str(response.status_code))
    log_event(log, "http", method=request.method, path=request.url.path, status=response.status_code,
              seconds=round(time.perf_counter() - started, 3))
    return response


# --- models -------------------------------------------------------------------------------
class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)


class QueryResponse(BaseModel):
    allowed: bool
    reason: str | None = None
    intent: str | None = None
    answer: str | None = None
    needs_human_review: bool = False
    degraded: bool = False
    review_reasons: list[str] = []
    escalation: str | None = None
    usage: dict = {}
    trace: list[dict] = []
    trace_id: str | None = None


class ResolveRequest(BaseModel):
    decision: Literal["approve", "reject"]


# --- routes -------------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    try:
        get_agent().store.get_customer("c1")  # touches the database
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {type(exc).__name__}")
    return {"status": "ready"}


@app.get("/metrics")
def prometheus_metrics() -> Response:
    # Restrict at the network layer (scraper-only) in production.
    return Response(metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, request: Request, actor: Actor = Depends(require_role("consumer", "wholesaler"))) -> QueryResponse:
    started = time.perf_counter()
    result = get_agent().handle(req.query, actor.role, actor.id, trace_id=request.state.trace_id)
    record_request(result, time.perf_counter() - started)
    return QueryResponse(**result)


@app.get("/ops/escalations")
def list_escalations(status: Literal["open", "approved", "rejected"] = "open",
                     actor: Actor = Depends(require_role("ops"))) -> list[dict]:
    return get_agent().store.escalations(status)


@app.post("/ops/escalations/{esc_id}/resolve")
def resolve_escalation(esc_id: int, body: ResolveRequest, actor: Actor = Depends(require_role("ops"))) -> dict:
    result = get_agent().store.resolve_escalation(esc_id, f"ops:{actor.id}", approve=body.decision == "approve")
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    metrics.inc("cartpilot_escalations_resolved_total", decision=body.decision)
    return result
