import hashlib
import json
import re
import unicodedata
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from rapidfuzz.fuzz import WRatio

from src.domain.models import Contract, Dataset
from src.infrastructure.source_scope import require_data_dir, require_source_file


def serialize(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"Tipo não serializável: {type(value).__name__}")


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=serialize)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_name(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", "".join(c for c in plain if not unicodedata.combining(c))).split())


def similarity(left: str, right: str) -> float:
    return WRatio(normalize_name(left), normalize_name(right))


def contract_id(contract: Contract) -> str:
    return f"{contract.source.file}!{contract.source.sheet}!{contract.source.ref}"


def contract_rows(data_dir: Path, dataset: Dataset) -> list[dict]:
    files = {contract.source.file for contract in dataset.contracts}
    if len(files) != 1:
        raise ValueError("Esperado um arquivo de contratos para normalização.")
    require_data_dir(data_dir)
    path = require_source_file(data_dir / next(iter(files)))
    workbook = load_workbook(path, read_only=False, data_only=False, keep_links=False)
    records = []
    try:
        for sheet in workbook:
            historical = sheet.title != "Taxas vigentes"
            first_row = 2 if historical else 6
            for cells in sheet.iter_rows(min_row=first_row):
                if not cells[0].value or not cells[1].value:
                    continue
                row = cells[0].row
                source_id = f"{path.name}!{sheet.title}!A{row}:{sheet.cell(row, sheet.max_column).column_letter}{row}"
                records.append({
                    "source_id": source_id, "historical": historical,
                    "merchant": str(cells[0].value),
                    "cnpj": re.sub(r"\D", "", str(cells[1].value)).zfill(14),
                    "establishment_code": None if historical or cells[2].value is None else str(cells[2].value),
                    "validity": None if historical else cells[10].value,
                    "note": None if historical else cells[11].value,
                    "cells": {c.coordinate: {"value": str(c.value) if isinstance(c.value, float) else c.value,
                        "format": c.number_format, "type": c.data_type,
                        "comment": c.comment.text if c.comment else None} for c in cells},
                })
    finally:
        workbook.close()
    current = {contract_id(c) for c in dataset.contracts}
    if {r["source_id"] for r in records if not r["historical"]} != current:
        raise ValueError("As linhas extraídas não correspondem aos contratos do leitor financeiro.")
    return records


def related_rows(focal: dict, rows: list[dict], threshold: float, limit: int) -> list[dict]:
    ranked = []
    for row in rows:
        exact = row["cnpj"] == focal["cnpj"] or bool(
            focal["establishment_code"] and row["establishment_code"] == focal["establishment_code"]
        )
        score = similarity(focal["merchant"], row["merchant"])
        if exact or score >= threshold:
            ranked.append((not exact, -score, row["source_id"], row))
    return [item[-1] for item in sorted(ranked)[:limit]]


def orbe_candidates(focal: dict, dataset: Dataset, threshold: float, sample_limit: int) -> list[dict]:
    groups: dict[str, list] = {}
    for entry in dataset.checkout:
        groups.setdefault(entry.establishment_code, []).append(entry)
    selected = []
    for ec, entries in sorted(groups.items()):
        score = max(similarity(focal["merchant"], entry.merchant) for entry in entries)
        if ec != focal["establishment_code"] and score < threshold:
            continue
        ordered = sorted(entries, key=lambda e: (e.occurred_on, e.source.ref))
        selected.append({
            "establishment_code": ec, "names": sorted({e.merchant for e in entries}),
            "name_score": round(score, 2),
            "total_rows": len(entries), "samples": [asdict(e) for e in ordered[:sample_limit]],
        })
    return selected


def document_chunks(data_dir: Path) -> list[dict]:
    require_data_dir(data_dir)
    chunks = []
    for path in sorted(data_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".csv", ".ofx", ".eml"}:
            continue
        require_source_file(path)
        if path.suffix.lower() == ".eml":
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            body = message.get_body(preferencelist=("plain",))
            content = body.get_content() if body else ""
            locator = "MIME:text/plain:"
            for attachment in message.iter_attachments():
                name = attachment.get_filename() or ""
                if not name.lower().endswith(".xlsx"):
                    continue
                name = name.replace("\\", "/").rsplit("/", 1)[-1]
                with BytesIO(attachment.get_payload(decode=True)) as payload:
                    workbook = load_workbook(payload, read_only=True, data_only=True, keep_links=False)
                    try:
                        for sheet in workbook:
                            for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                                if any(value is not None for value in row):
                                    chunks.append({"source_id": f"{path.name}!{name}!{sheet.title}:L{number}",
                                        "file": path.name, "text": json_text(list(row))})
                    finally:
                        workbook.close()
        else:
            content = path.read_text(encoding="cp1252" if path.suffix.lower() == ".ofx" or path.name.startswith("04_") else "utf-8-sig")
            locator = ""
        lines = content.splitlines()
        for start in range(0, len(lines), 12):
            chunks.append({"source_id": f"{path.name}:{locator}L{start + 1}-L{min(start + 12, len(lines))}",
                "file": path.name, "text": "\n".join(lines[start:start + 12])})
    return chunks


def retrieve_documents(chunks: list[dict], focal: dict, ec: str, limit: int = 24) -> list[dict]:
    terms = {focal["cnpj"], ec, normalize_name(focal["merchant"])} - {""}
    ranked = []
    for chunk in chunks:
        plain = normalize_name(chunk["text"])
        digits = re.sub(r"\D", "", chunk["text"])
        score = sum(5 for term in terms if term in plain or term in digits)
        if score:
            ranked.append((-score, chunk["source_id"], chunk))
    return [item[-1] for item in sorted(ranked)[:limit]]


def input_manifest(data_dir: Path) -> dict[str, str]:
    require_data_dir(data_dir)
    files = sorted(p for p in data_dir.iterdir() if p.is_file())
    return {f"dados/{p.name}": digest(require_source_file(p).read_bytes()) for p in files}
