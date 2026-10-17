import json
from pathlib import Path

from src.infrastructure.flow_log import FlowLog
from src.infrastructure.normalization_sources import digest, json_text
from src.infrastructure.source_scope import SOURCE_SCOPE


def convert_cache_log(cache_dir: Path, output_dir: Path) -> dict:
    with FlowLog(output_dir) as log:
        if not cache_dir.is_dir():
            raise ValueError("Diretório de cache inexistente.")
        paths = sorted(cache_dir.glob("*.json"))
        if not paths:
            raise ValueError("Nenhum registro JSON encontrado no cache.")
        imported, rejected = 0, 0
        log.event("CACHE", "CONVERSAO_INICIADA", cache_files=len(paths))
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or not isinstance(record.get("request"), dict) or not isinstance(record.get("response"), dict):
                    raise ValueError("Registro de cache inválido.")
                if record["request"].get("source_scope") != SOURCE_SCOPE:
                    raise ValueError("Cache anterior ou com fontes externas; conversão recusada.")
                if digest(json_text(record["request"]).encode("utf-8")) != path.stem:
                    raise ValueError("Hash do cache não corresponde à solicitação.")
            except (OSError, UnicodeError, ValueError, TypeError) as error:
                rejected += 1
                log.event("CACHE", "CONVERSAO_REJEITADA", level="WARNING", cache_file=path.name,
                    error_type=type(error).__name__)
                continue
            log.event("CACHE", "CACHE_CONVERTIDO", cache_file=path.name,
                request_hash=path.stem, cache_record=record)
            imported += 1
        summary = {"cache_files": len(paths), "converted": imported, "rejected": rejected}
        log.event("CACHE", "CONVERSAO_CONCLUIDA", **summary)
        return summary
