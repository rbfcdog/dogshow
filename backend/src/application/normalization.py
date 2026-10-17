import csv
import json
import re
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from src.application.checkout import _rate_key, _reference
from src.domain.models import Contract, Dataset
from src.domain.normalization import ContractProposal, Investigation
from src.domain.policies import ESTABLISHMENT_BINDINGS, checkout_exempt, contract_candidates
from src.infrastructure.normalization_sources import (
    contract_id, contract_rows, document_chunks, email_chunks, input_manifest, json_text,
    orbe_candidates, related_rows, retrieve_documents,
)


class Interpreter(Protocol):
    model: str

    def invoke(self, context: dict, stage: str) -> ContractProposal | Investigation: ...


def explicit_bounds(validity: str | None) -> tuple[date | None, date | None]:
    value = (validity or "").strip()
    match = re.fullmatch(r"(até |a partir de )?(\d{2}/\d{2}/\d{4})", value, re.IGNORECASE)
    if not match:
        raise ValueError("Vigência sem interpretação temporal verificável; investigar a fonte.")
    day = datetime.strptime(match[2], "%d/%m/%Y").date()
    return (None, day) if (match[1] or "").lower() == "até " else (day, None)


def context_for(
    focal: dict, rows: list[dict], dataset: Dataset, threshold: float, samples: int, limit: int,
    chunks: list[dict] | None = None,
) -> dict:
    candidates = related_rows(focal, rows, threshold, limit)
    candidates = [focal] + [r for r in candidates if r["source_id"] != focal["source_id"]][:limit - 1]
    context = {
        "focal": focal, "related_contracts": candidates,
        "orbe_groups": orbe_candidates(focal, dataset, threshold, samples),
        "selection": {"fuzzy_threshold": threshold, "samples_per_ec": samples,
            "max_related_contracts": limit, "orbe_has_cnpj_column": False},
    }
    if chunks:
        docs = retrieve_documents(chunks, focal, focal.get("establishment_code") or "")
        emails = [c for c in chunks if c["file"].endswith(".eml")]
        if emails and not any(d["file"].endswith(".eml") for d in docs):
            docs = docs + emails[:4]
        context["documents"] = docs
        context["searched_documents"] = sorted({c["file"] for c in chunks})
    return context


def evidence_ids(context: dict) -> set[str]:
    samples = [sample for group in context["orbe_groups"] for sample in group["samples"]]
    return {context["focal"]["source_id"]} | {row["source_id"] for row in context["related_contracts"]} | {
        row["source_id"] for row in context.get("documents", [])
    } | {sample["source"]["ref"] for sample in samples} | {
        f"{sample['source']['file']}!{sample['source']['ref']}!row{sample['source']['row']}" for sample in samples
    }


def proposal_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
            raise ValueError("Data deve estar em AAAA-MM-DD ou DD/MM/AAAA.") from None
        return datetime.strptime(value, "%d/%m/%Y").date()


def validate_proposal(proposal: ContractProposal, context: dict, rows: list[dict]) -> None:
    available = {r["source_id"]: r for r in context["related_contracts"]}
    if proposal.source_id not in available or available[proposal.source_id]["historical"]:
        raise ValueError("Proposta deve referenciar uma linha contratual atual do contexto.")
    if not proposal.evidence_ids or proposal.source_id not in proposal.evidence_ids:
        raise ValueError("Falta evidência da própria linha contratual.")
    if not set(proposal.evidence_ids) <= evidence_ids(context):
        raise ValueError("Proposta cita evidência inexistente no contexto.")
    actual = {r["source_id"]: r for r in rows}
    if proposal.source_id not in actual or available[proposal.source_id] != actual[proposal.source_id]:
        raise ValueError("Evidência contratual não corresponde à extração dos originais.")
    row = actual[proposal.source_id]
    if proposal.disposition != "UTILIZAVEL":
        return
    if "INATIVO" in (row["note"] or "").upper():
        raise ValueError("Contrato marcado inativo não pode ser promovido a utilizável.")
    try:
        start = proposal_date(proposal.validity_start)
        end = proposal_date(proposal.validity_end)
    except ValueError:
        raise ValueError("Datas propostas inválidas.") from None
    if (start, end) != explicit_bounds(row["validity"]):
        raise ValueError("Vigência proposta não corresponde à evidência temporal da linha.")
    if start and end and start > end:
        raise ValueError("Intervalo de vigência invertido.")
    ec = proposal.establishment_code
    if row["establishment_code"] and ec != row["establishment_code"]:
        raise ValueError("A proposta altera um EC explicitamente preenchido na fonte.")
    confirmed = [key for key, cnpj in ESTABLISHMENT_BINDINGS.items() if cnpj == row["cnpj"]]
    if confirmed and ec not in confirmed:
        raise ValueError("A proposta contradiz vínculo confirmado pelo financeiro.")
    if ec and not row["establishment_code"]:
        groups = {g["establishment_code"] for g in context["orbe_groups"]}
        if ec not in groups:
            raise ValueError("EC inferido não foi encontrado entre os candidatos Orbe selecionados.")
    for other in rows:
        if ec and not other["historical"] and other["establishment_code"] == ec and other["cnpj"] != row["cnpj"]:
            raise ValueError("EC proposto já pertence a outro CNPJ explicitamente documentado.")


def materialize(dataset: Dataset, decisions: dict[str, dict], rows: list[dict]) -> tuple[Dataset, list[dict]]:
    originals = {contract_id(c): c for c in dataset.contracts}
    merged: dict[str, tuple[Contract, dict]] = {}
    for source_id, record in sorted(decisions.items()):
        proposal = ContractProposal.model_validate(record["proposal"])
        if source_id != proposal.source_id:
            raise ValueError("Chave de decisão não corresponde à origem da proposta.")
        validate_proposal(proposal, record["context"], rows)
        if proposal.disposition != "UTILIZAVEL":
            continue
        raw = originals[source_id]
        contract = replace(raw, establishment_code=proposal.establishment_code,
            validity_start=proposal_date(proposal.validity_start),
            validity_end=proposal_date(proposal.validity_end),
            structured_validity=True)
        signature = json_text({"cnpj": contract.cnpj, "ec": contract.establishment_code,
            "rates": contract.rates, "remittance_rate": contract.remittance_rate,
            "start": contract.validity_start, "end": contract.validity_end})
        status = "DOCUMENTADO" if raw.establishment_code or ESTABLISHMENT_BINDINGS.get(
            contract.establishment_code or "") == contract.cnpj else "INFERIDO" if contract.establishment_code else "DOCUMENTADO"
        if signature in merged:
            canonical = merged[signature][1]
            canonical["source_ids"].append(source_id)
            canonical["evidence_ids"] = sorted(set(canonical["evidence_ids"]) | set(proposal.evidence_ids))
        else:
            merged[signature] = contract, {"source_id": source_id, "source_ids": [source_id],
                "merchant": contract.merchant, "cnpj": contract.cnpj, "establishment_code": contract.establishment_code,
                "rates": contract.rates, "remittance_rate": contract.remittance_rate,
                "validity_start": contract.validity_start.isoformat() if contract.validity_start else None,
                "validity_end": contract.validity_end.isoformat() if contract.validity_end else None,
                "status": status, "evidence_ids": proposal.evidence_ids, "reason": proposal.reason}
    return replace(dataset, contracts=tuple(value[0] for value in merged.values())), [value[1] for value in merged.values()]


def join_checkout(dataset: Dataset) -> list[dict]:
    originals = {entry.source.ref: entry for entry in dataset.checkout}
    if len(originals) != len(dataset.checkout):
        raise ValueError("ID Orbe repetido; o join não pode sobrescrever operações.")
    joined = []
    for entry in dataset.checkout:
        row = {"ref_origem": entry.source.ref, "ec": entry.establishment_code,
            "data_referencia": None, "modalidade": None, "cnpj": None,
            "contrato": None, "taxa_contratada_fracao": None, "status": "PENDENTE", "erro": None}
        try:
            original = _reference(entry, originals)
            on_date = original.occurred_on if original is not entry or entry.kind in {
                "VENDA", "ANTECIPACAO"} else entry.original_on
            if on_date is None:
                raise ValueError("Data histórica indeterminada.")
            row["data_referencia"] = on_date.isoformat()
            key = _rate_key(original)
            row["modalidade"] = key
            candidates = contract_candidates(dataset.contracts, on_date, establishment_code=entry.establishment_code)
            if len(candidates) != 1:
                raise ValueError(f"Encontrados {len(candidates)} contratos aplicáveis; esperado um.")
            contract = candidates[0]
            if not checkout_exempt(contract, on_date) and key not in contract.rates:
                raise ValueError(f"Contrato sem taxa para {key}.")
            row.update(cnpj=contract.cnpj, contrato=contract_id(contract), status="OK",
                taxa_contratada_fracao="0" if checkout_exempt(contract, on_date) else str(contract.rates[key]))
        except ValueError as error:
            row["erro"] = str(error)
        joined.append(row)
    return joined


def unresolved_groups(joined: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in joined:
        if row["status"] != "OK":
            groups[row["ec"]].append(row)
    return dict(sorted(groups.items()))


def write_json(path: Path, content: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json_text(content) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize_reference(dataset: Dataset, data_dir: Path, docs_dir: Path, output_dir: Path,
    interpreter: Interpreter, *, samples: int = 5, threshold: float = 75, candidate_limit: int = 8) -> dict:
    if samples < 1 or candidate_limit < 1 or not 0 <= threshold <= 100:
        raise ValueError("Limites de candidatos/amostras devem ser positivos e score entre 0 e 100.")
    if output_dir.resolve() == data_dir.resolve() or data_dir.resolve() in output_dir.resolve().parents:
        raise ValueError("Artefatos devem ficar fora do diretório de originais.")
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = contract_rows(data_dir, dataset)
    chunks = document_chunks(docs_dir) + email_chunks(data_dir)
    decisions: dict[str, dict] = {}
    normalizations = []
    for focal in (r for r in rows if not r["historical"]):
        context = context_for(focal, rows, dataset, threshold, samples, candidate_limit, chunks)
        proposal = interpreter.invoke(context, "NORMALIZACAO")
        if not isinstance(proposal, ContractProposal):
            raise ValueError("Saída do interpretador não corresponde ao schema de normalização.")
        error = None
        try:
            if proposal.source_id != focal["source_id"]:
                raise ValueError("A normalização deve responder apenas pela linha focal.")
            validate_proposal(proposal, context, rows)
            decisions[proposal.source_id] = {"proposal": proposal.model_dump(), "context": context}
        except ValueError as failure:
            error = str(failure)
        normalizations.append({"source_id": focal["source_id"], "proposal": proposal.model_dump(), "validation_error": error})
    normalized, canonical = materialize(dataset, decisions, rows)
    joined = join_checkout(normalized)
    investigations = []
    attempted: set[str] = set()
    while True:
        remaining = {ec: problems for ec, problems in unresolved_groups(joined).items() if ec not in attempted}
        if not remaining:
            break
        ec, problems = next(iter(remaining.items()))
        attempted.add(ec)
        entries = [e for e in dataset.checkout if e.establishment_code == ec]
        focal = {"source_id": f"orbe:EC:{ec}", "merchant": entries[0].merchant,
            "cnpj": ESTABLISHMENT_BINDINGS.get(ec, ""), "establishment_code": ec, "historical": False}
        candidates = related_rows(focal, rows, threshold, candidate_limit)
        context = {
            "focal": focal, "failures": problems, "related_contracts": candidates,
            "orbe_groups": orbe_candidates(focal, dataset, threshold, samples),
            "documents": retrieve_documents(chunks, focal, ec),
            "searched_documents": sorted({c["file"] for c in chunks}),
            "normalization_attempts": [n for n in normalizations if n["source_id"] in {r["source_id"] for r in candidates}],
        }
        result = interpreter.invoke(context, "RCA")
        if not isinstance(result, Investigation):
            raise ValueError("Saída do interpretador não corresponde ao schema de investigação.")
        applied, rejected = [], []
        if len({p.source_id for p in result.proposals}) != len(result.proposals):
            rejected.append("RCA contém propostas repetidas para a mesma linha; nenhuma aplicada.")
        elif not set(result.evidence_ids) <= evidence_ids(context):
            rejected.append("Diagnóstico cita evidência inexistente; nenhuma proposta aplicada.")
        else:
            for proposal in result.proposals:
                try:
                    validate_proposal(proposal, context, rows)
                    prior = decisions.get(proposal.source_id)
                    if prior and prior["proposal"]["disposition"] == "UTILIZAVEL" and prior["proposal"]["establishment_code"] not in {None, ec}:
                        raise ValueError("RCA não pode substituir vínculo aceito de outro EC.")
                    if proposal.disposition == "UTILIZAVEL" and proposal.establishment_code != ec:
                        raise ValueError("Correção de RCA deve resolver o EC investigado.")
                    decisions[proposal.source_id] = {"proposal": proposal.model_dump(), "context": context}
                    applied.append(proposal.source_id)
                except ValueError as failure:
                    rejected.append(str(failure))
        normalized, canonical = materialize(dataset, decisions, rows)
        joined = join_checkout(normalized)
        investigations.append({"ec": ec, "context": context, "result": result.model_dump(),
            "applied_source_ids": applied, "rejected": rejected,
            "resolved": ec not in unresolved_groups(joined)})
    escalations = []
    for ec, problems in unresolved_groups(joined).items():
        investigation = next(item for item in investigations if item["ec"] == ec)
        escalations.append({"ec": ec, "status": "ESCALAR_HUMANO", "quem_decide": "Comercial / Diego",
            "operacoes": [p["ref_origem"] for p in problems], "falhas": problems,
            "diagnostico": investigation["result"]["diagnosis"],
            "hipoteses": investigation["result"]["hypotheses"],
            "informacao_faltante": investigation["result"]["missing_information"] or "Resolver falhas de validação/contrato descritas no diagnóstico.",
            "fontes_pesquisadas": investigation["context"]["searched_documents"],
            "trechos_consultados": investigation["context"]["documents"],
            "tentativas_rejeitadas": investigation["rejected"],
            "impacto_comissao_brl": None, "restante_join_processado": True})
    report = {"version": 1, "model": interpreter.model, "input_hashes": input_manifest(data_dir, docs_dir),
        "settings": {"samples": samples, "threshold": threshold, "candidate_limit": candidate_limit},
        "decisions": decisions, "normalization": normalizations,
        "summary": {"contract_rows": len(normalizations), "canonical_rows": len(canonical),
            "checkout_rows": len(joined), "joined": sum(r["status"] == "OK" for r in joined),
            "unresolved_rows": sum(r["status"] != "OK" for r in joined),
            "rca_cases": len(investigations), "human_cases": len(escalations)},
        "unusable_contracts": [source_id for source_id in {contract_id(c) for c in dataset.contracts}
            if source_id not in decisions or decisions[source_id]["proposal"]["disposition"] != "UTILIZAVEL"]}
    export_reference(output_dir, report, canonical, joined, investigations, escalations)
    return report


def export_reference(output_dir: Path, report: dict, canonical: list[dict], joined: list[dict],
    investigations: list[dict], escalations: list[dict]) -> None:
    write_json(output_dir / "referencias.json", report)
    write_json(output_dir / "rca.json", investigations)
    write_json(output_dir / "escaladas_humanas.json", escalations)
    fields = ["source_id", "cnpj", "establishment_code", "merchant", "modalidade", "taxa_fracao",
        "validity_start", "validity_end", "status", "source_ids", "evidence_ids"]
    with (output_dir / "contratos_normalizados.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";", lineterminator="\n")
        writer.writeheader()
        for contract in canonical:
            for modality, rate in sorted((contract["rates"] | {"REMESSA": contract["remittance_rate"]}).items()):
                row = {k: contract.get(k) for k in fields}
                row.update(modalidade=modality, taxa_fracao=rate,
                    source_ids=json_text(contract["source_ids"]), evidence_ids=json_text(contract["evidence_ids"]))
                writer.writerow(row)
    with (output_dir / "join_checkout.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = list(joined[0]) if joined else ["ref_origem", "ec", "status", "erro"]
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";", lineterminator="\n")
        writer.writeheader()
        writer.writerows(joined)


def load_reference(dataset: Dataset, data_dir: Path, reference_file: Path) -> Dataset:
    report = json.loads(reference_file.read_text(encoding="utf-8"))
    if report.get("version") != 1:
        raise ValueError("Versão de referência não suportada.")
    actual = input_manifest(data_dir, Path("/__tarvo_no_docs__"))
    expected = {k: v for k, v in report["input_hashes"].items() if k.startswith("dados/")}
    if actual != expected:
        raise ValueError("Referência incompatível com o pacote atual; hashes das fontes divergentes.")
    rows = contract_rows(data_dir, dataset)
    return materialize(dataset, report["decisions"], rows)[0]
