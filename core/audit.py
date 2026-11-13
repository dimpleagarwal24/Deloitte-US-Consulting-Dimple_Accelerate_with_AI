import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from core.config import AUDIT_DIR


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid4().hex[:12]}"


class AuditLogger:
    def __init__(self, run_id: str | None = None):
        self.run_id = new_run_id() if run_id is None else run_id
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.run_id):
            raise ValueError("Invalid run_id")
        self.log_path: Path = AUDIT_DIR / f"{self.run_id}.jsonl"

    def log(self, agent: str, action: str, **kwargs) -> None:
        entry = {
            **kwargs,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "agent": agent,
            "action": action,
        }
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, allow_nan=False) + "\n")

    def get_logs(self) -> list[dict]:
        try:
            with self.log_path.open(encoding="utf-8") as stream:
                return [json.loads(line) for line in stream if line.strip()]
        except FileNotFoundError:
            return []