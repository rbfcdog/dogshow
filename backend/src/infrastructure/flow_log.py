import json
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from src.infrastructure.normalization_sources import serialize


class FlowLog:
    def __init__(self, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        self.path = output_dir / "fluxo.log"
        self.run_id = uuid4().hex
        self.sequence = 0
        self.started = monotonic()

    def event(self, stage: str, event: str, *, level: str = "INFO", **details) -> None:
        self.sequence += 1
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id, "sequence": self.sequence,
            "elapsed_ms": round((monotonic() - self.started) * 1000),
            "level": level, "stage": stage, "event": event, "details": details,
        }
        with self.path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=serialize) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def __enter__(self):
        self.event("EXECUCAO", "EXECUCAO_INICIADA")
        return self

    def __exit__(self, error_type, error, traceback):
        if error is None:
            self.event("EXECUCAO", "EXECUCAO_CONCLUIDA")
        else:
            self.event("EXECUCAO", "EXECUCAO_FALHOU", level="ERROR", error_type=error_type.__name__)
        return False
