import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

OUTPUT_DIR = Path(os.getenv("PIPELINE_OUTPUT_DIR", str(BASE_DIR))).resolve()
LANDING_DIR = OUTPUT_DIR / "data" / "landing"
PROFILES_DIR = OUTPUT_DIR / "data" / "profiles"
STTM_DIR = OUTPUT_DIR / "data" / "sttm"
BRONZE_DIR = OUTPUT_DIR / "data" / "bronze_layer"
SILVER_DIR = OUTPUT_DIR / "data" / "silver_layer"
GOLD_DIR = OUTPUT_DIR / "data" / "gold_layer"
TRACES_DIR = OUTPUT_DIR / "data" / "traces"
REPORTS_DIR = OUTPUT_DIR / "reports"
AUDIT_DIR = OUTPUT_DIR / "audit_logs"

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "github").strip().lower()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_BASE_URL = "https://models.github.ai/inference"
GITHUB_MODEL = os.getenv("GITHUB_MODEL", "openai/gpt-4.1-mini").strip()
if "/" not in GITHUB_MODEL:
    GITHUB_MODEL = f"openai/{GITHUB_MODEL}"


def ensure_dirs() -> None:
    for directory in (
        LANDING_DIR, PROFILES_DIR, STTM_DIR, BRONZE_DIR, SILVER_DIR,
        GOLD_DIR, TRACES_DIR, REPORTS_DIR, AUDIT_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)