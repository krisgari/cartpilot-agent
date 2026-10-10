"""Mint a test JWT:  python scripts/make_token.py consumer c1   (needs CARTPILOT_JWT_SECRET in .env)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cartpilot.config import settings  # noqa: E402
from cartpilot.security import ROLES, encode_token  # noqa: E402

if len(sys.argv) < 3 or sys.argv[1] not in ROLES:
    sys.exit(f"usage: make_token.py <{'|'.join(sorted(ROLES))}> <actor-id> [ttl-minutes]")
if not settings.jwt_secret:
    sys.exit("set CARTPILOT_JWT_SECRET first")
ttl = int(sys.argv[3]) if len(sys.argv) > 3 else 60
claims = {"sub": sys.argv[2], "role": sys.argv[1], "exp": int(time.time()) + ttl * 60}
if settings.jwt_issuer:
    claims["iss"] = settings.jwt_issuer
if settings.jwt_audience:
    claims["aud"] = settings.jwt_audience
print(encode_token(claims, settings.jwt_secret))
