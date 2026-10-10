import base64
import json
import time
from types import SimpleNamespace

from cartpilot.security import Actor, AuthError, decode_token, encode_token, resolve_actor

SECRET = "s3cret"


def _cfg(**kw):
    base = dict(jwt_secret=SECRET, jwt_issuer="", jwt_audience="", env="production")
    return SimpleNamespace(**{**base, **kw})


def _tok(**claims):
    return encode_token({"sub": "c1", "role": "consumer", "exp": int(time.time()) + 600, **claims}, SECRET)


def _fails(fn, text):
    try:
        fn()
    except AuthError as exc:
        assert text in str(exc), str(exc)
        return exc
    raise AssertionError("expected AuthError")


def test_valid_token_resolves_actor():
    assert resolve_actor(f"Bearer {_tok()}", cfg=_cfg()) == Actor("c1", "consumer")


def test_tampered_payload_rejected():
    h, b, s = _tok().split(".")
    forged = base64.urlsafe_b64encode(json.dumps({"sub": "c2", "role": "ops", "exp": int(time.time()) + 600}).encode()).rstrip(b"=").decode()
    _fails(lambda: decode_token(f"{h}.{forged}.{s}", SECRET), "signature")


def test_wrong_secret_rejected():
    _fails(lambda: decode_token(encode_token({"sub": "c1", "role": "consumer", "exp": int(time.time()) + 60}, "other"), SECRET), "signature")


def test_alg_none_rejected():
    hdr = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps({"sub": "x", "role": "ops", "exp": int(time.time()) + 60}).encode()).rstrip(b"=").decode()
    _fails(lambda: decode_token(f"{hdr}.{body}.", SECRET), "algorithm")


def test_expired_and_missing_exp_rejected():
    _fails(lambda: decode_token(_tok(exp=int(time.time()) - 3600), SECRET), "expired")
    no_exp = encode_token({"sub": "c1", "role": "consumer"}, SECRET)
    _fails(lambda: decode_token(no_exp, SECRET), "no expiry")


def test_issuer_and_audience_enforced():
    _fails(lambda: decode_token(_tok(), SECRET, issuer="https://idp"), "issuer")
    _fails(lambda: decode_token(_tok(iss="https://idp"), SECRET, issuer="https://idp", audience="cartpilot"), "audience")
    assert decode_token(_tok(iss="https://idp", aud=["cartpilot", "x"]), SECRET, issuer="https://idp", audience="cartpilot")


def test_role_must_be_valid():
    _fails(lambda: resolve_actor(f"Bearer {_tok(role='admin')}", cfg=_cfg()), "role")


def test_missing_or_malformed_header_rejected():
    _fails(lambda: resolve_actor(None, cfg=_cfg()), "missing bearer")
    _fails(lambda: resolve_actor("Bearer not.a.jwt", cfg=_cfg()), "")


def test_fail_closed_without_secret():
    exc = _fails(lambda: resolve_actor(None, cfg=_cfg(jwt_secret="")), "not configured")
    assert exc.status == 503
    # the dev header is ignored outside dev mode, even if sent
    _fails(lambda: resolve_actor(None, "ops:evil", cfg=_cfg(jwt_secret="", env="production")), "not configured")


def test_dev_header_only_in_dev_mode():
    assert resolve_actor(None, "consumer:c1", cfg=_cfg(jwt_secret="", env="dev")) == Actor("c1", "consumer")
    _fails(lambda: resolve_actor(None, "root:x", cfg=_cfg(jwt_secret="", env="dev")), "X-Dev-Actor")


def test_token_wins_over_dev_header_when_secret_set():
    # a client cannot downgrade to the header path by sending it alongside (or instead of) a token
    _fails(lambda: resolve_actor(None, "ops:evil", cfg=_cfg(env="dev")), "missing bearer")
