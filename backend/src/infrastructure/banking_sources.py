import re
from collections import defaultdict
from dataclasses import asdict, fields
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from src.domain.closing import ClassifiedMovement, Pending
from src.domain.models import BankEntry, Dataset, RemittanceEntry, SourceRef
from src.domain.policies import money, output_id
from src.infrastructure.normalization_sources import digest, normalize_name
from src.infrastructure.parsing import cnpj
_CNPJ = re.compile(r"(?<![0-9])(?:[0-9]{2}\.[0-9]{3}\.[0-9]{3}/[0-9]{4}-[0-9]{2}|[0-9]{14})(?![0-9])")
_GENERIC = {
    "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em", "o", "os", "para", "por",
    "ltda", "sa", "me", "eireli", "banco", "aurora", "meridiano", "cnpj", "pix", "ted",
    "receb", "recebido", "recebida", "enviado", "enviada", "pagto", "pagamento", "deb", "aut",
    "devolucao", "transferencia", "transf", "entre", "contas", "mesma", "titularidade",
    "liquidacao", "liq", "sem", "identificacao",
}
_ECONOMIC_REMITTANCE_FIELDS = tuple(
    field.name for field in fields(RemittanceEntry) if field.name not in {"source", "note"}
)


def _source_id(source: SourceRef) -> str:
    parts = [source.file, source.sheet or "", source.ref]
    if source.row is not None:
        parts.append(f"L{source.row}")
    return "!".join(parts)


def markdown_chunks(docs_dir: Path) -> list[dict]:
    chunks = []
    if not docs_dir.exists():
        return chunks
    for path in sorted(docs_dir.glob("*.md")):
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        for start in range(0, len(lines), 12):
            chunks.append({
                "source_id": f"{path.name}:L{start + 1}-L{min(start + 12, len(lines))}",
                "file": path.name,
                "text": "\n".join(lines[start:start + 12]),
            })
    return chunks


def bank_manifest(data_dir: Path, docs_dir: Path) -> dict[str, str]:
    files = sorted(p for p in data_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    documents = sorted(
        p for p in (docs_dir.glob("*.md") if docs_dir.exists() else [])
        if not p.name.startswith(".")
    )
    return {f"dados/{p.name}": digest(p.read_bytes()) for p in files} | {
        f"docs/{p.name}": digest(p.read_bytes()) for p in documents
    }


def _cnpjs(value: str, source: SourceRef) -> set[str]:
    return {cnpj(match.group(), source) for match in _CNPJ.finditer(value)}


def _terms(value: str) -> set[str]:
    return {word for word in normalize_name(value).split() if len(word) >= 3 and word.isalpha()} - _GENERIC


def _name_candidate(terms: set[str], value: str, expanded: bool) -> bool:
    words = _terms(value)
    shared = words & terms
    return bool(words) and (
        words <= terms or len(shared) >= 2 or expanded and any(len(word) >= 6 for word in shared)
    )


def _annotate_chunks(chunks: list[dict]) -> list[dict]:
    annotated = []
    by_file: dict[str, list[dict]] = defaultdict(list)
    for chunk in chunks:
        by_file[chunk["file"]].append(chunk)
    for file_name, rows in by_file.items():
        section = ""
        for chunk in rows:
            lines = [line for line in chunk["text"].splitlines() if line.strip()]
            if lines and (lines[0].startswith("## ") or lines[0].startswith("### ")):
                section = lines[0].lstrip("#").strip()
            chunk_section = section
            for line in lines[1:]:
                if line.startswith("## ") or line.startswith("### "):
                    section = line.lstrip("#").strip()
            plain_text = normalize_name(chunk["text"])
            plain_section = normalize_name(chunk_section)
            if file_name.startswith("PERGUNTA_"):
                authority = "PERGUNTA_PARTICIPANTE_SEM_CONFIRMACAO"
            elif (
                "nao viraram confirmacao" in plain_section
                or "premissas adotadas pelo time" in plain_section
                or "como eu lancaria" in plain_text
            ):
                authority = "PREMISSA_DO_TIME"
            elif (
                "fatos e criterios confirmados" in plain_section
                or "confirmado pelo financeiro" in plain_section
            ):
                authority = "FINANCEIRO_CONFIRMADO"
            else:
                authority = "DOCUMENTO_PROJETO"
            annotated.append({**chunk, "section": chunk_section, "authority": authority})
    return annotated


def _documents(chunks: list[dict], entry: BankEntry, issues: list[Pending]) -> list[dict]:
    identifiers = _cnpjs(entry.description, entry.source)
    terms = set(normalize_name(entry.description).split()) - {"de", "da", "do", "e", "sem", "cnpj"}
    for issue in issues:
        terms.update(normalize_name(issue.tipo).split())
    terms = {term for term in terms if len(term) >= 3 and term.isalpha()}
    if "orbe" in terms:
        terms |= {"liquidacao", "creditos", "recebiveis", "adquirente", "agenda"}
    if {"ponte", "cambio"} & terms:
        terms |= {"remessa", "remessas", "liquidar", "recursos", "comissao", "spread"}
    if "devolucao" in terms:
        terms |= {"repasse", "repasses", "voltou", "motivo"}
    if {"transf", "transferencia", "titularidade"} & terms:
        terms |= {"transferencia", "proprias", "transito", "outubro"}
    amount = abs(entry.amount)
    localized = format(amount, ",.2f").translate(str.maketrans({",": ".", ".": ","}))
    amounts = {format(amount, "f"), localized}
    if amount == amount.to_integral_value():
        amounts.add(localized.split(",")[0])
    ranked = []
    for chunk in _annotate_chunks(chunks):
        text = chunk["text"]
        words = set(normalize_name(text).split())
        section_words = set(normalize_name(chunk.get("section", "")).split())
        exact = bool(identifiers & _cnpjs(text, entry.source)) or bool(
            re.search(rf"(?<!\w){re.escape(entry.source.ref)}(?!\w)", text, re.IGNORECASE)
        )
        overlap = len(terms & words)
        score = 8 * any(re.search(rf"(?<![0-9]){re.escape(value)}(?![0-9])", text) for value in amounts)
        score += overlap
        if overlap and {"bancos", "bancarios", "orbe", "remessas", "premissas"} & section_words:
            score += 4
        if overlap and chunk["authority"] == "FINANCEIRO_CONFIRMADO":
            score += 3
        elif overlap and chunk["authority"] == "PREMISSA_DO_TIME":
            score += 2
        elif chunk["authority"] == "PERGUNTA_PARTICIPANTE_SEM_CONFIRMACAO" and not exact:
            continue
        if exact or score:
            ranked.append((not exact, -score, chunk["source_id"], chunk))
    return [item[-1] for item in sorted(ranked, key=lambda item: item[:3])]


def _daily_remittance_candidates(dataset: Dataset, entry: BankEntry) -> list[dict]:
    plain = normalize_name(entry.description)
    if entry.bank != "MERIDIANO" or entry.amount >= 0 or "ponte cambio" not in plain:
        return []
    groups: dict[str, list[RemittanceEntry]] = defaultdict(list)
    for record in dataset.remittances:
        if record.contracted_on == entry.occurred_on:
            groups[record.source.ref].append(record)
    if not groups:
        return []
    unique: list[ tuple[RemittanceEntry, int, bool] ] = []
    has_conflict = False
    for _, occurrences in sorted(groups.items()):
        occurrences.sort(key=lambda row: (row.source.file, row.source.sheet or "", row.source.row or 0))
        payloads = {tuple(getattr(row, name) for name in _ECONOMIC_REMITTANCE_FIELDS) for row in occurrences}
        conflict = len(payloads) != 1
        has_conflict = has_conflict or conflict
        unique.append((occurrences[0], len(occurrences) - 1, conflict))
    daily_total = sum(
        (money(record.brl_amount + record.iof) for record, _, conflict in unique if not conflict),
        Decimal("0.00"),
    )
    ted_abs = abs(money(entry.amount))
    return [
        {
            "source_id": _source_id(record.source),
            "kind": "REMITTANCE",
            "match_basis": "DAILY_REMITTANCE_CANDIDATE",
            "principal_plus_iof": money(record.brl_amount + record.iof),
            "daily_remittance_count": len(unique),
            "daily_remittance_total_brl": daily_total,
            "daily_bank_ted_brl": ted_abs,
            "daily_aggregate_matches_ted": not has_conflict and daily_total == ted_abs,
            "duplicate_occurrences_ignored": duplicates,
            "conflicting_occurrences": conflict,
            "scope_note": (
                "Comparação diária agregada por data de contratação (principal + IOF das operações únicas); "
                "evidência de contexto para liquidação cambial em 2.1.02.001, não vínculo individual por operação."
            ),
            "payload": asdict(record),
        }
        for record, duplicates, conflict in unique
    ]

def bank_context(
    dataset: Dataset, movement: ClassifiedMovement, issues: list[Pending], chunks: list[dict], *, rca: bool = False,
) -> dict:
    entries = [entry for entry in dataset.banks if output_id(
        "movimento", entry.occurred_on, entry.bank, entry.source.file, entry.source.ref,
    ) == movement.id_saida]
    if len(entries) != 1:
        raise ValueError("Movimento bancário sem origem única no conjunto de dados.")
    entry = entries[0]
    if (entry.bank, entry.source.ref, entry.occurred_on.isoformat(), entry.description, money(entry.amount)) != (
        movement.banco, movement.ref_origem, movement.data, movement.descricao, movement.valor,
    ):
        raise ValueError("Movimento bancário incompatível com os dados originais.")
    identifiers = _cnpjs(entry.description, entry.source)
    terms = _terms(entry.description)
    related = []
    seen_related: set[str] = set()
    for kind, records in (("CONTRACT", dataset.contracts), ("REMITTANCE", dataset.remittances)):
        for record in records:
            exact = record.cnpj in identifiers
            if exact or _name_candidate(terms, record.merchant, rca):
                source_id = _source_id(record.source)
                seen_related.add(source_id)
                item = {
                    "source_id": source_id, "kind": kind,
                    "match_basis": "EXACT_CNPJ" if exact else "NAME_CANDIDATE", "payload": asdict(record),
                }
                if kind == "REMITTANCE":
                    item["principal_plus_iof"] = money(record.brl_amount + record.iof)
                related.append(item)
    for candidate_item in _daily_remittance_candidates(dataset, entry):
        if candidate_item["source_id"] not in seen_related:
            seen_related.add(candidate_item["source_id"])
            related.append(candidate_item)
    for candidate in dataset.banks:
        if candidate is entry:
            continue
        exact = bool(identifiers & _cnpjs(candidate.description, candidate.source))
        name = _name_candidate(terms, candidate.description, rca)
        counterpart = (
            entry.amount != 0 and candidate.bank != entry.bank and candidate.amount == -entry.amount
            and abs(candidate.occurred_on - entry.occurred_on) <= timedelta(days=7 if rca else 0)
        )
        if exact or name or counterpart:
            related.append({
                "source_id": _source_id(candidate.source), "kind": "BANK",
                "match_basis": "EXACT_CNPJ" if exact else "NAME_CANDIDATE" if name else "COUNTERPART_CANDIDATE",
                "payload": asdict(candidate),
            })
    annotated_chunks = _annotate_chunks(chunks)
    ranked = _documents(chunks, entry, issues)
    selected = ranked[:24 if rca else 12]
    seed_ids = {chunk["source_id"] for chunk in (selected if rca else selected[:4])}
    by_file: dict[str, list[dict]] = defaultdict(list)
    for chunk in annotated_chunks:
        by_file[chunk["file"]].append(chunk)
    for siblings in by_file.values():
        for index, chunk in enumerate(siblings):
            if chunk["source_id"] in seed_ids:
                selected.extend(
                    neighbor for neighbor in siblings[max(0, index - 1):index + 2]
                    if neighbor["authority"] != "PERGUNTA_PARTICIPANTE_SEM_CONFIRMACAO"
                )
    selected = list({chunk["source_id"]: chunk for chunk in selected}.values())
    return {
        "movement": {
            "movement_id": movement.id_saida, "source_id": movement.id_saida, "bank": entry.bank,
            "ref": entry.source.ref, "source": asdict(entry.source), "data": entry.occurred_on.isoformat(),
            "description": entry.description, "amount": entry.amount,
        },
        "baseline": asdict(movement), "issues": [asdict(issue) for issue in issues],
        "accounts": [{"source_id": _source_id(account.source), **asdict(account)} for account in dataset.accounts],
        "related_records": sorted(related, key=lambda record: (record["match_basis"] != "EXACT_CNPJ", record["source_id"])),
        "documents": selected, "searched_documents": sorted({chunk["file"] for chunk in chunks}),
    }
