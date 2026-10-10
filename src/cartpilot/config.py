"""Settings, read from environment (.env is loaded if python-dotenv is installed)."""
import os
from dataclasses import dataclass
from pathlib import Path

try:  # optional: tests and pure-logic modules don't need dotenv
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    model: str = os.getenv("CARTPILOT_MODEL", "claude-sonnet-4-5-20250929")
    # Identity. "production" (default) is FAIL-CLOSED: requests are rejected unless a JWT secret is configured
    # and the token verifies. "dev" additionally accepts an X-Dev-Actor header so curl demos work locally.
    env: str = os.getenv("CARTPILOT_ENV", "production")
    jwt_secret: str = os.getenv("CARTPILOT_JWT_SECRET", "")
    jwt_issuer: str = os.getenv("CARTPILOT_JWT_ISSUER", "")      # if set, tokens must carry this iss
    jwt_audience: str = os.getenv("CARTPILOT_JWT_AUDIENCE", "")  # if set, tokens must carry this aud
    seed_path: Path = Path(os.getenv("CARTPILOT_SEED", ROOT / "data" / "seed.json"))
    state_path: Path = Path(os.getenv("CARTPILOT_STATE", ROOT / "data" / "state.db"))

    # Refund policy limits (deterministic code enforces these, never the LLM)
    auto_refund_limit: float = float(os.getenv("CARTPILOT_AUTO_REFUND_LIMIT", "25"))
    max_auto_refunds_30d: int = int(os.getenv("CARTPILOT_MAX_AUTO_REFUNDS_30D", "3"))


settings = Settings()
