"""
Identity: who is calling, and in what role. Framework-free so it is unit-testable.

Tokens are HS256 JWTs verified with the standard library:
  * the algorithm is pinned (a token claiming alg=none or RS256 is rejected, never "trusted");
  * `exp` is REQUIRED; `iss` / `aud` are checked when configured;
  * signatures are compared in constant time.
Claims used:  sub = actor id (customer id, wholesaler id, or ops user),  role = consumer | wholesaler | ops.

To integrate a real identity provider that signs with RS256 (Auth0, Okta, Cognito, Entra ID), replace
`decode_token` with PyJWT + the provider's JWKS endpoint; nothing else in the app changes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

from cartpilot.config import settings

log = logging.getLogger("cartpilot.security")
ROLES = {"consumer", "wholesaler", "ops"}
LEEWAY_SECONDS = 30


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Actor:
    id: str
    role: str  # consumer | wholesaler | ops


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_token(claims: dict, secret: str) -> str:
    """Mint an HS256 token. Used by scripts/make_token.py and tests; real tokens come from your IdP."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def decode_token(token: str, secret: str, *, issuer: str = "", audience: str = "", now: float | None = None) -> dict:
    try:
        header_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(_unb64(header_b64))
        claims = json.loads(_unb64(body_b64))
        sig = _unb64(sig_b64)
    except Exception as exc:
        raise AuthError("malformed token") from exc

    if header.get("alg") != "HS256":  # pinned: blocks alg=none and algorithm-confusion attacks
        raise AuthError("unsupported token algorithm")
    expected = hmac.new(secret.encode(), f"{header_b64}.{body_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise AuthError("invalid token signature")

    t = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise AuthError("token has no expiry")
    if t > exp + LEEWAY_SECONDS:
        raise AuthError("token expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and t < nbf - LEEWAY_SECONDS:
        raise AuthError("token not yet valid")
    if issuer and claims.get("iss") != issuer:
        raise AuthError("wrong token issuer")
    if audience:
        aud = claims.get("aud")
        if audience not in (aud if isinstance(aud, list) else [aud]):
            raise AuthError("wrong token audience")
    return claims


def resolve_actor(authorization: str | None, dev_actor: str | None = None, *, cfg=None) -> Actor:
    """Authorization header ('Bearer <jwt>') -> Actor. Raises AuthError (401/503). Fail-closed."""
    cfg = cfg or settings  # looked up at call time so tests can patch cartpilot.security.settings
    if cfg.jwt_secret:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("missing bearer token")
        claims = decode_token(authorization[7:].strip(), cfg.jwt_secret, issuer=cfg.jwt_issuer, audience=cfg.jwt_audience)
        sub, role = claims.get("sub"), claims.get("role")
        if not sub or role not in ROLES:
            raise AuthError("token is missing a valid sub/role")
        return Actor(str(sub), role)

    if cfg.env == "dev" and dev_actor:  # local demos only
        role, _, sub = dev_actor.partition(":")
        if role in ROLES and sub:
            log.warning("DEV MODE: trusting X-Dev-Actor header (never enable in production)")
            return Actor(sub, role)
        raise AuthError("X-Dev-Actor must look like 'consumer:c1'")

    raise AuthError("authentication is not configured (set CARTPILOT_JWT_SECRET)", status=503)  # fail closed
