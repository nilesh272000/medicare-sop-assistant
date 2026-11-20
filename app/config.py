"""All configuration comes from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (no dependency). Real environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    provider: str                  # "fake" | "gemini"
    gemini_api_key: str
    gemini_chat_model: str
    gemini_embed_model: str
    # USD per 1M tokens, used for per-call cost logging.
    price_in_per_m: float
    price_out_per_m: float
    price_embed_per_m: float
    db_path: str
    access_policy_path: str
    staff_users_path: str
    top_k: int
    max_context_docs: int
    min_retrieval_score: float     # below this the question is refused without calling the LLM
    llm_timeout_s: float
    embed_timeout_s: float
    max_retries: int
    cache_ttl_s: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(os.getenv("ENV_FILE", ".env"))
        e = os.environ.get
        provider = e("LLM_PROVIDER", "fake").lower()
        # The refusal threshold is a property of the embedding model's score distribution,
        # so it has a per-provider default (calibrated on the eval set; see docs/eval_results.md).
        default_min = "0.10" if provider == "fake" else "0.45"
        return cls(
            provider=provider,
            gemini_api_key=e("GEMINI_API_KEY", ""),
            gemini_chat_model=e("GEMINI_CHAT_MODEL", "gemini-2.5-flash"),
            gemini_embed_model=e("GEMINI_EMBED_MODEL", "gemini-embedding-001"),
            price_in_per_m=float(e("PRICE_INPUT_PER_M_TOKENS", "0.30")),
            price_out_per_m=float(e("PRICE_OUTPUT_PER_M_TOKENS", "2.50")),
            price_embed_per_m=float(e("PRICE_EMBED_PER_M_TOKENS", "0.15")),
            db_path=e("DB_PATH", "var/sop_index.sqlite3"),
            access_policy_path=e("ACCESS_POLICY_PATH", "data/access_policy.json"),
            staff_users_path=e("STAFF_USERS_PATH", "data/staff_users.csv"),
            top_k=int(e("TOP_K", "8")),
            max_context_docs=int(e("MAX_CONTEXT_DOCS", "3")),
            min_retrieval_score=float(e("MIN_RETRIEVAL_SCORE", default_min)),
            llm_timeout_s=float(e("LLM_TIMEOUT_S", "20")),
            embed_timeout_s=float(e("EMBED_TIMEOUT_S", "10")),
            max_retries=int(e("MAX_RETRIES", "2")),
            cache_ttl_s=int(e("CACHE_TTL_S", str(7 * 24 * 3600))),
            log_level=e("LOG_LEVEL", "INFO"),
        )
