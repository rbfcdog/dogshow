import csv
import json
import re
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from src.application.checkout import _rate_key, _reference
from src.domain.models import Contract, Dataset
from src.domain.normalization import ContractProposal, Investigation
from src.domain.policies import contract_candidates
from src.infrastructure.flow_log import FlowLog
from src.infrastructure.source_scope import SOURCE_SCOPE
from src.infrastructure.normalization_sources import (
    bank_candidates, contract_id, contract_rows, document_chunks, email_chunks, input_manifest,
    json_text, orbe_candidates, partner_email_evidence, related_rows, remittance_candidates,
    retrieve_documents,
)
from src.infrastructure.slack_inbox import SlackInbox, find_inbox_file, parse_inbox


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
) -> dict:
    candidates = related_rows(focal, rows, threshold, limit)
    candidates = [focal] + [r for r in candidates if r["source_id"] != focal["source_id"]][:limit - 1]
    return {
        "source_scope": SOURCE_SCOPE,
        "focal": focal, "related_contracts": candidates,
        "orbe_groups": orbe_candidates(focal, dataset, threshold, samples),
        "remittance_groups": remittance_candidates(focal, dataset, threshold, samples, candidates),
        "partner_email": partner_email_evidence(dataset),
        "bank_mentions": bank_candidates(focal, dataset, threshold, samples, candidates),
        "selection": {"fuzzy_threshold": threshold, "samples_per_ec": samples,
            "max_related_contracts": limit, "orbe_has_cnpj_column": False},
    }


def evidence_ids(context: dict) -> set[str]:
    samples = [sample for group in context["orbe_groups"] for sample in group["samples"]]
    remittance_samples = [
        sample for group in context.get("remittance_groups", []) for sample in group.get("samples", [])
    ]
    partner = context.get("partner_email")
    partner_ids = {partner["source_id"], partner["file"]} if isinstance(partner, dict) and "source_id" in partner else set()
    bank_ids = {
        identifier for item in context.get("bank_mentions", [])
        for identifier in (item.get("source_id"), item.get("ref")) if identifier
    }
    return (
        {context["focal"]["source_id"]}
        | {row["source_id"] for row in context["related_contracts"]}
        | {row["source_id"] for row in context.get("documents", [])}
        | {sample["source"]["ref"] for sample in samples}
        | {f"{sample['source']['file']}:{sample['source']['ref']}" for sample in samples}
        | {f"{sample['source']['file']}:L{sample['source']['row']}" for sample in samples}
        | {f"{sample['source']['file']}!{sample['source']['ref']}!row{sample['source']['row']}" for sample in samples}
        | {sample["source_id"] for sample in remittance_samples}
        | {sample["ref"] for sample in remittance_samples}
        | partner_ids
        | bank_ids
    )

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
    if ec and not row["establishment_code"]:
        groups = {g["establishment_code"] for g in context["orbe_groups"]}
        if ec not in groups:
            raise ValueError("EC inferido não foi encontrado entre os candidatos Orbe selecionados.")
    for other in rows:
        if ec and not other["historical"] and other["establishment_code"] == ec and other["cnpj"] != row["cnpj"]:
            raise ValueError("EC proposto já pertence a outro CNPJ explicitamente documentado.")


def materialize(dataset: Dataset, decisions: dict[str, dict], rows: list[dict],
    inbox: SlackInbox | None = None) -> tuple[Dataset, list[dict]]:
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
        ec = proposal.establishment_code
        evidence_ids = list(proposal.evidence_ids)
        reason = proposal.reason
        status = "INFERIDO" if ec and not raw.establishment_code else "DOCUMENTADO"

        if inbox and not ec:
            for binding_ec, binding in inbox.bindings.items():
                if binding.cnpj == raw.cnpj:
                    ec = binding_ec
                    evidence_ids.append(binding.source_id)
                    reason = f"{reason} {binding.reason}".strip()
                    status = "CONFIRMADO_INBOX"
                    break

        contract = replace(raw, establishment_code=ec,
            validity_start=proposal_date(proposal.validity_start),
            validity_end=proposal_date(proposal.validity_end),
            structured_validity=True)
        signature = json_text({"cnpj": contract.cnpj, "ec": contract.establishment_code,
            "rates": contract.rates, "remittance_rate": contract.remittance_rate,
            "start": contract.validity_start, "end": contract.validity_end})
        if signature in merged:
            canonical = merged[signature][1]
            canonical["source_ids"].append(source_id)
            canonical["evidence_ids"] = sorted(set(canonical["evidence_ids"]) | set(evidence_ids))
        else:
            merged[signature] = contract, {"source_id": source_id, "source_ids": [source_id],
                "merchant": contract.merchant, "cnpj": contract.cnpj, "establishment_code": contract.establishment_code,
                "rates": contract.rates, "remittance_rate": contract.remittance_rate,
                "validity_start": contract.validity_start.isoformat() if contract.validity_start else None,
                "validity_end": contract.validity_end.isoformat() if contract.validity_end else None,
                "status": status, "evidence_ids": sorted(set(evidence_ids)), "reason": reason}

    if inbox and inbox.exemptions:
        for cnpj, ex in inbox.exemptions.items():
            matching_signatures = [sig for sig, (c, _) in merged.items() if c.cnpj == cnpj]
            for sig in matching_signatures:
                base_contract, base_canonical = merged[sig]
                if ex.preserve_historical_sales and ex.start_date:
                    hist_contract = replace(base_contract, validity_end=ex.start_date - timedelta(days=1), note=None, comments=None)
                    hist_sig = json_text({"cnpj": hist_contract.cnpj, "ec": hist_contract.establishment_code,
                        "rates": hist_contract.rates, "remittance_rate": hist_contract.remittance_rate,
                        "start": hist_contract.validity_start, "end": hist_contract.validity_end})
                    merged[hist_sig] = hist_contract, {**base_canonical,
                        "validity_end": hist_contract.validity_end.isoformat(),
                        "reason": f"{base_canonical['reason']} Vigência anterior à isenção da caixa de entrada."}
                exempt_rates = {k: ex.rate if k in ex.modalities else v for k, v in base_contract.rates.items()}
                exempt_contract = replace(base_contract, rates=exempt_rates,
                    validity_start=ex.start_date, validity_end=ex.end_date, note=None, comments=None)
                exempt_sig = json_text({"cnpj": exempt_contract.cnpj, "ec": exempt_contract.establishment_code,
                    "rates": exempt_contract.rates, "remittance_rate": exempt_contract.remittance_rate,
                    "start": exempt_contract.validity_start, "end": exempt_contract.validity_end})
                merged[exempt_sig] = exempt_contract, {
                    "source_id": f"{base_canonical['source_id']}#isencao",
                    "source_ids": base_canonical["source_ids"],
                    "merchant": exempt_contract.merchant,
                    "cnpj": exempt_contract.cnpj,
                    "establishment_code": exempt_contract.establishment_code,
                    "rates": exempt_contract.rates,
                    "remittance_rate": exempt_contract.remittance_rate,
                    "validity_start": exempt_contract.validity_start.isoformat(),
                    "validity_end": exempt_contract.validity_end.isoformat() if exempt_contract.validity_end else None,
                    "status": "ISENTO_INBOX",
                    "evidence_ids": sorted(set(base_canonical["evidence_ids"]) | {ex.source_id}),
                    "reason": ex.reason,
                }
                del merged[sig]

    return replace(dataset, contracts=tuple(value[0] for value in merged.values())), [value[1] for value in merged.values()]


def join_checkout(dataset: Dataset, inbox: SlackInbox | None = None) -> list[dict]:
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
                if inbox and entry.establishment_code in inbox.blocks:
                    raise ValueError(inbox.blocks[entry.establishment_code].reason)
                raise ValueError(f"Encontrados {len(candidates)} contratos aplicáveis; esperado um.")
            contract = candidates[0]
            if key not in contract.rates:
                raise ValueError(f"Contrato sem taxa para {key}.")
            row.update(cnpj=contract.cnpj, contrato=contract_id(contract), status="OK",
                taxa_contratada_fracao=str(contract.rates[key]))
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


def validate_output_dir(data_dir: Path, output_dir: Path) -> None:
    if output_dir.resolve() == data_dir.resolve() or data_dir.resolve() in output_dir.resolve().parents:
        raise ValueError("Artefatos devem ficar fora do diretório de originais.")


def normalize_reference(dataset: Dataset, data_dir: Path, output_dir: Path,
    interpreter: Interpreter, *, samples: int = 5, threshold: float = 75, candidate_limit: int = 8,
    log: FlowLog | None = None, inbox: SlackInbox | None = None,
    inbox_path: Path | None = None) -> dict:
    validate_output_dir(data_dir, output_dir)
    resolved_inbox = inbox
    if resolved_inbox is None and inbox_path is not None:
        resolved_inbox = parse_inbox(inbox_path)
    if log is None:
        with FlowLog(output_dir) as run_log:
            return _normalize_reference(dataset, data_dir, output_dir, interpreter,
                samples=samples, threshold=threshold, candidate_limit=candidate_limit, log=run_log, inbox=resolved_inbox)
    return _normalize_reference(dataset, data_dir, output_dir, interpreter,
        samples=samples, threshold=threshold, candidate_limit=candidate_limit, log=log, inbox=resolved_inbox)


def _normalize_reference(dataset: Dataset, data_dir: Path, output_dir: Path,
    interpreter: Interpreter, *, samples: int, threshold: float, candidate_limit: int, log: FlowLog,
    inbox: SlackInbox | None = None) -> dict:
    if samples < 1 or candidate_limit < 1 or not 0 <= threshold <= 100:
        raise ValueError("Limites de candidatos/amostras devem ser positivos e score entre 0 e 100.")
    output_dir.mkdir(parents=True, exist_ok=True)
    log.event("INGESTAO", "EXTRACAO_CONTRATUAL_INICIADA")
    rows = contract_rows(data_dir, dataset)
    data_chunks = document_chunks(data_dir)
    chunks = data_chunks + (inbox.chunks if inbox else [])
    if inbox:
        log.event("INGESTAO", "CAIXA_DE_ENTRADA_PROCESSADA",
            source_file=inbox.source_file,
            bindings=len(inbox.bindings),
            exemptions=len(inbox.exemptions),
            blocks=len(inbox.blocks))
    log.event("INGESTAO", "EXTRACAO_CONTRATUAL_CONCLUIDA", current_rows=sum(not r["historical"] for r in rows),
        historical_rows=sum(r["historical"] for r in rows), document_chunks=len(chunks))
    decisions: dict[str, dict] = {}
    normalizations = []
    log.event("NORMALIZACAO", "NORMALIZACAO_INICIADA", model=interpreter.model)
    for focal in (r for r in rows if not r["historical"]):
        context = context_for(focal, rows, dataset, threshold, samples, candidate_limit)
        log.event("NORMALIZACAO", "CONTEXTO_SELECIONADO", source_id=focal["source_id"],
            related_contracts=len(context["related_contracts"]),
            candidate_ecs=[g["establishment_code"] for g in context["orbe_groups"]],
            sample_rows=sum(len(g["samples"]) for g in context["orbe_groups"]))
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
        log.event("NORMALIZACAO", "PROPOSTA_REJEITADA" if error else "PROPOSTA_VALIDADA",
            level="WARNING" if error else "INFO", source_id=focal["source_id"],
            disposition=proposal.disposition, ec=proposal.establishment_code,
            reason=proposal.reason, validation_error=error, evidence_ids=proposal.evidence_ids)
    normalized, canonical = materialize(dataset, decisions, rows, inbox=inbox)
    log.event("NORMALIZACAO", "NORMALIZACAO_CONCLUIDA", canonical_rows=len(canonical))
    log.event("JOIN", "JOIN_INICIADO", checkout_rows=len(dataset.checkout))
    joined = join_checkout(normalized, inbox=inbox)
    log.event("JOIN", "JOIN_CONCLUIDO", joined=sum(r["status"] == "OK" for r in joined),
        unresolved_by_ec={ec: len(problems) for ec, problems in unresolved_groups(joined).items()})
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
            "cnpj": "", "establishment_code": ec, "historical": False}
        candidates = related_rows(focal, rows, threshold, candidate_limit)
        context = {
            "source_scope": SOURCE_SCOPE,
            "focal": focal, "failures": problems, "related_contracts": candidates,
            "orbe_groups": orbe_candidates(focal, dataset, threshold, samples),
            "remittance_groups": remittance_candidates(focal, dataset, threshold, samples, candidates),
            "partner_email": partner_email_evidence(dataset),
            "bank_mentions": bank_candidates(focal, dataset, threshold, samples, candidates),
            "documents": retrieve_documents(chunks, focal, ec),
            "searched_documents": sorted({c["file"] for c in data_chunks}),
            "normalization_attempts": [n for n in normalizations if n["source_id"] in {r["source_id"] for r in candidates}],
        }
        log.event("RCA", "INVESTIGACAO_INICIADA", ec=ec, affected_rows=len(problems),
            contract_candidates=len(candidates), document_ids=[d["source_id"] for d in context["documents"]])
        result = interpreter.invoke(context, "RCA")
        if not isinstance(result, Investigation):
            raise ValueError("Saída do interpretador não corresponde ao schema de investigação.")
        log.event("RCA", "DIAGNOSTICO_RECEBIDO", ec=ec, diagnosis=result.diagnosis,
            hypotheses=result.hypotheses, missing_information=result.missing_information,
            proposed_corrections=len(result.proposals), evidence_ids=result.evidence_ids)
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
        normalized, canonical = materialize(dataset, decisions, rows, inbox=inbox)
        log.event("JOIN", "REJOIN_INICIADO", ec=ec, applied_source_ids=applied)
        joined = join_checkout(normalized, inbox=inbox)
        log.event("JOIN", "REJOIN_CONCLUIDO", ec=ec,
            unresolved_rows=len(unresolved_groups(joined).get(ec, [])))
        investigations.append({"ec": ec, "context": context, "result": result.model_dump(),
            "applied_source_ids": applied, "rejected": rejected,
            "resolved": ec not in unresolved_groups(joined)})
        log.event("RCA", "INVESTIGACAO_CONCLUIDA", ec=ec, applied_source_ids=applied,
            rejected=rejected, resolved=investigations[-1]["resolved"])
    escalations = []
    for ec, problems in unresolved_groups(joined).items():
        investigation = next(item for item in investigations if item["ec"] == ec)
        escalations.append({"ec": ec, "status": "ESCALAR_HUMANO", "quem_decide": "Comercial / Financeiro",
            "operacoes": [p["ref_origem"] for p in problems], "falhas": problems,
            "diagnostico": investigation["result"]["diagnosis"],
            "hipoteses": investigation["result"]["hypotheses"],
            "informacao_faltante": investigation["result"]["missing_information"] or "Resolver falhas de validação/contrato descritas no diagnóstico.",
            "evidencias_rca": investigation["result"]["evidence_ids"],
            "contratos_relacionados": [r["source_id"] for r in investigation["context"]["related_contracts"]],
            "remessas_relacionadas": [g["cnpj"] for g in investigation["context"].get("remittance_groups", [])],
            "bancos_relacionados": [b["source_id"] for b in investigation["context"].get("bank_mentions", [])],
            "fontes_pesquisadas": investigation["context"]["searched_documents"],
            "trechos_consultados": investigation["context"]["documents"],
            "tentativas_rejeitadas": investigation["rejected"],
            "impacto_comissao_brl": None, "restante_join_processado": True})
        log.event("ESCALADA", "CASO_ESCALADO", level="WARNING", ec=ec,
            affected_rows=len(problems), responsible=escalations[-1]["quem_decide"],
            missing_information=escalations[-1]["informacao_faltante"])
    report = {"version": 2, "source_scope": SOURCE_SCOPE, "model": interpreter.model, "input_hashes": input_manifest(data_dir),
        "settings": {"samples": samples, "threshold": threshold, "candidate_limit": candidate_limit},
        "decisions": decisions, "normalization": normalizations,
        "summary": {"contract_rows": len(normalizations), "canonical_rows": len(canonical),
            "checkout_rows": len(joined), "joined": sum(r["status"] == "OK" for r in joined),
            "unresolved_rows": sum(r["status"] != "OK" for r in joined),
            "rca_cases": len(investigations), "human_cases": len(escalations)},
        "unusable_contracts": [source_id for source_id in {contract_id(c) for c in dataset.contracts}
            if source_id not in decisions or decisions[source_id]["proposal"]["disposition"] != "UTILIZAVEL"]}
    log.event("EXPORTACAO", "EXPORTACAO_INICIADA")
    export_reference(output_dir, report, canonical, joined, investigations, escalations)
    log.event("EXPORTACAO", "EXPORTACAO_CONCLUIDA", artifacts=["referencias.json", "rca.json",
        "escaladas_humanas.json", "contratos_normalizados.csv", "join_checkout.csv"])
    log.event("EXECUCAO", "RESUMO", **report["summary"])
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
def apply_inbox_policies(dataset: Dataset, inbox: SlackInbox | None) -> Dataset:
    if inbox is None:
        return dataset
    contracts = list(dataset.contracts)
    if inbox.bindings:
        for idx, contract in enumerate(contracts):
            if contract.establishment_code is None:
                for binding_ec, binding in inbox.bindings.items():
                    if binding.cnpj == contract.cnpj:
                        contracts[idx] = replace(contract, establishment_code=binding_ec)
                        break
    if inbox.exemptions:
        new_contracts = []
        for contract in contracts:
            if contract.cnpj in inbox.exemptions:
                ex = inbox.exemptions[contract.cnpj]
                start_date = contract.validity_start or date(2026, 1, 1)
                if ex.preserve_historical_sales and ex.start_date:
                    hist_contract = replace(
                        contract,
                        validity_start=start_date,
                        validity_end=ex.start_date - timedelta(days=1),
                        structured_validity=True,
                        note=None,
                        comments=None,
                    )
                    new_contracts.append(hist_contract)
                exempt_rates = {k: ex.rate if k in ex.modalities else v for k, v in contract.rates.items()}
                exempt_contract = replace(
                    contract,
                    rates=exempt_rates,
                    validity_start=ex.start_date,
                    validity_end=ex.end_date,
                    structured_validity=True,
                    note=None,
                    comments=None,
                )
                new_contracts.append(exempt_contract)
            else:
                new_contracts.append(contract)
        contracts = new_contracts
    return replace(dataset, contracts=tuple(contracts))



def load_reference(dataset: Dataset, data_dir: Path, reference_file: Path,
    inbox: SlackInbox | None = None) -> Dataset:
    report = json.loads(reference_file.read_text(encoding="utf-8"))
    if report.get("version") != 2 or report.get("source_scope") != SOURCE_SCOPE:
        raise ValueError("Referência anterior ou fora do escopo dados_tarvo; gere uma nova normalização.")
    actual = input_manifest(data_dir)
    expected = report["input_hashes"]
    if actual != expected:
        raise ValueError("Referência incompatível com o pacote atual; hashes das fontes divergentes.")
    rows = contract_rows(data_dir, dataset)
    chunks = document_chunks(data_dir)
    settings = report["settings"]
    originals = {r["source_id"]: r for r in rows if not r["historical"]}
    safe_decisions = {}
    for source_id, record in report["decisions"].items():
        saved = record["context"]
        if saved.get("source_scope") != SOURCE_SCOPE:
            raise ValueError("Contexto de referência fora do escopo permitido.")
        focal_id = saved["focal"]["source_id"]
        if focal_id in originals:
            fresh = context_for(originals[focal_id], rows, dataset, settings["threshold"], settings["samples"], settings["candidate_limit"])
        else:
            ec = saved["focal"].get("establishment_code")
            entries = [e for e in dataset.checkout if e.establishment_code == ec]
            if not entries or focal_id != f"orbe:EC:{ec}":
                raise ValueError("EC de investigação não existe na fonte.")
            focal = {"source_id": focal_id, "merchant": entries[0].merchant, "cnpj": "", "establishment_code": ec, "historical": False}
            candidates = related_rows(focal, rows, settings["threshold"], settings["candidate_limit"])
            fresh = {"source_scope": SOURCE_SCOPE, "focal": focal,
                "related_contracts": candidates,
                "orbe_groups": orbe_candidates(focal, dataset, settings["threshold"], settings["samples"]),
                "remittance_groups": remittance_candidates(focal, dataset, settings["threshold"], settings["samples"], candidates),
                "partner_email": partner_email_evidence(dataset),
                "bank_mentions": bank_candidates(focal, dataset, settings["threshold"], settings["samples"], candidates),
                "documents": retrieve_documents(chunks, focal, ec),
                "searched_documents": sorted({c["file"] for c in chunks})}
        if any(json_text(saved.get(key)) != json_text(value) for key, value in fresh.items()):
            raise ValueError("Contexto de referência não corresponde às evidências atuais de dados_tarvo.")
        safe_decisions[source_id] = {"proposal": record["proposal"], "context": fresh}
    return materialize(dataset, safe_decisions, rows, inbox=inbox)[0]
