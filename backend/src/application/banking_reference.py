import csv
import json
import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from src.application.banking import classify_banks
from src.application.normalization import write_json
from src.domain.banking_reference import BankInvestigation, BankProposal, ReviewedBankDecision
from src.domain.models import Dataset
from src.infrastructure.banking_sources import bank_context, bank_manifest as input_manifest, markdown_chunks as document_chunks
from src.infrastructure.normalization_sources import digest, json_text

def _hash(value: object) -> str:
    return digest(json_text(value).encode("utf-8"))


def _period(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Competência inválida: use AAAA-MM.")
    return date.fromisoformat(value + "-01")


def _pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Referência contém chaves JSON duplicadas.")
        result[key] = value
    return result


def _read(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    if not isinstance(result, dict):
        raise ValueError("Referência deve ser um objeto JSON.")
    return result


def _outside_sources(path: Path, data_dir: Path, docs_dir: Path) -> None:
    for source in (data_dir, docs_dir):
        if path.resolve() == source.resolve() or source.resolve() in path.resolve().parents:
            raise ValueError("Artefatos devem ficar fora dos diretórios de originais e documentos.")


def _cases(dataset: Dataset, period: date) -> dict:
    if not isinstance(period, date) or period.day != 1:
        raise ValueError("Competência deve ser o primeiro dia do mês.")
    baseline = classify_banks(dataset, period)
    movements = {movement.id_saida: movement for movement in baseline.movements}
    if len(movements) != len(baseline.movements):
        raise ValueError("Movimentos bancários duplicados.")
    if not movements:
        raise ValueError("Nenhum movimento bancário na competência informada.")
    traces = {item.id_saida: item for item in baseline.traces}
    def origin(item):
        return item.arquivo_origem, item.ref_origem, item.linha_origem, item.aba_origem
    pending = {}
    for item in baseline.pending:
        if item.tipo != "VALIDAR_RECONHECIMENTO_CONTABIL":
            pending.setdefault(origin(traces[item.id_saida]), []).append(item)
    return {
        identifier: (movement, pending.get(origin(traces[identifier]), []))
        for identifier, movement in movements.items()
        if not movement.conta_codigo or origin(traces[identifier]) in pending
    }


def _template_key(movement) -> str | None:
    description = movement.descricao.upper()
    if movement.banco == "AURORA" and movement.valor < 0 and description.startswith("PIX ENVIADO "):
        return "AURORA:PIX_ENVIADO"
    if (
        movement.banco == "MERIDIANO" and movement.valor > 0
        and description.startswith("PIX RECEB") and "SEM IDENTIFICACAO" not in description
    ):
        return "MERIDIANO:PIX_RECEB"
    return None
def _evidence(context: dict) -> set[str]:
    return {context["movement"]["source_id"]} | {
        row["source_id"] for key in ("accounts", "related_records", "documents")
        for row in context.get(key, [])}


def _validate(proposal: BankProposal, context: dict, movement, dataset: Dataset) -> None:
    if proposal.movement_id != movement.id_saida:
        raise ValueError("Proposta deve identificar apenas o movimento focal elegível.")
    cited = set(proposal.evidence_ids)
    if not proposal.reason.strip() or context["movement"]["source_id"] not in cited:
        raise ValueError("Proposta exige justificativa e evidência do próprio movimento.")
    if len(cited) != len(proposal.evidence_ids) or not cited <= _evidence(context):
        raise ValueError("Proposta contém evidência duplicada ou inexistente no contexto real.")
    if proposal.account_code is None:
        return
    actual = [account for account in dataset.accounts if account.code == proposal.account_code]
    if len(actual) != 1 or actual[0].kind != "ANALITICA" or not actual[0].posting_allowed:
        raise ValueError("Conta não é única e lançável no plano original.")
    accounts = [row for row in context["accounts"] if row["code"] == proposal.account_code]
    if len(accounts) != 1 or accounts[0]["kind"] != "ANALITICA" or accounts[0]["posting_allowed"] is not True:
        raise ValueError("Conta deve existir de forma única, ser ANALITICA e aceitar lançamento.")
    if accounts[0]["source_id"] not in cited:
        raise ValueError("Proposta deve citar a evidência da conta proposta.")
    if movement.confianca == Decimal("1") and proposal.account_code != movement.conta_codigo:
        raise ValueError("Proposta não pode substituir conta determinística confirmada.")



_ISSUE_ACTIONS = {
    "IDENTIFICAR_PIX": (
        "Solicitar ao Financeiro e ao Banco Aurora o comprovante, a identificação do pagador "
        "e a finalidade do PIX (CNPJ na descrição bancária não comprova contrato, remessa ou baixa de título). "
        "Manter em 1.1.09.002 (Créditos não identificados — decisão fundamentada do time) sem reconhecer receita."
    ),
    "MOTIVO_DEVOLUCAO_PIX": (
        "Solicitar ao Financeiro o código/motivo bancário da devolução do PIX de repasse e "
        "confirmar com Mundo Aberto os dados bancários para reenvio. Manter em 2.1.01.001 "
        "(Repasses a lojistas a pagar — decisão fundamentada do time)."
    ),
    "TRANSFERENCIA_EM_TRANSITO": (
        "Solicitar ao Financeiro o extrato do Banco Meridiano de outubro/2026 para comprovar "
        "o crédito de R$ 300.000,00. Manter a saída de 30/09/2026 em "
        "1.1.09.001 (Transferências entre contas próprias), sem reclassificar como perda ou despesa."
    ),
    "CONCILIAR_TRANSFERENCIA_PROPRIA": (
        "Solicitar comprovante bancário da transferência entre contas próprias e conciliar a ponta "
        "correspondente no outro extrato antes de encerrar a pendência."
    ),
    "CLASSIFICACAO_BANCARIA": (
        "Definir com o Financeiro a conta analítica lançável de contrapartida no plano Fênix ERP "
        "com documento de suporte que comprove a natureza da operação."
    ),
    "CONTA_NAO_LANCAVEL": (
        "Substituir a conta bloqueada/sintética por conta ANALITICA que aceite lançamento no plano "
        "Fênix ERP após validação com o Financeiro."
    ),
}


def _resolve_evidences(context: dict, cited_ids: list[str]) -> list[dict]:
    valid = _evidence(context)
    index: dict[str, dict] = {}
    mov = context["movement"]
    index[mov["source_id"]] = {
        "source_id": mov["source_id"],
        "tipo": "MOVIMENTO_BANCARIO",
        "autoridade": "FONTE_PRIMARIA_EXTRATO",
        "localizacao": f"{mov['source']['file']}:{mov['ref']}",
        "resumo": f"{mov['bank']} {mov['data']} {mov['ref']} ({format(mov['amount'], 'f')}): {mov['description']}",
    }
    for acc in context.get("accounts", []):
        index[acc["source_id"]] = {
            "source_id": acc["source_id"],
            "tipo": "PLANO_DE_CONTAS",
            "autoridade": "FONTE_PRIMARIA_ERP",
            "localizacao": acc["source_id"],
            "resumo": f"{acc['code']} — {acc['description']} ({acc['kind']}, aceita_lancto={acc['posting_allowed']})",
        }
    for rel in context.get("related_records", []):
        index[rel["source_id"]] = {
            "source_id": rel["source_id"],
            "tipo": f"REGISTRO_RELACIONADO_{rel['kind']}",
            "autoridade": f"CANDIDATO_{rel['match_basis']}",
            "localizacao": rel["source_id"],
            "resumo": rel.get("scope_note") or f"Candidato {rel['kind']} por {rel['match_basis']}",
        }
    for doc in context.get("documents", []):
        excerpt = " ".join(line.strip() for line in doc["text"].splitlines() if line.strip())
        index[doc["source_id"]] = {
            "source_id": doc["source_id"],
            "tipo": "DOCUMENTO",
            "autoridade": doc.get("authority", "DOCUMENTO_PROJETO"),
            "localizacao": doc["source_id"],
            "resumo": excerpt[:240],
        }
    resolved = []
    for source_id in dict.fromkeys(cited_ids):
        if source_id in valid and source_id in index:
            resolved.append(index[source_id])
    return resolved


def _build_escalation(
    identifier: str, movement, issues: list, context: dict, accepted: dict | None,
    accepted_stage: str | None, investigation: dict | None, missing: list[str],
    errors: list[str], output_dir: Path,
) -> dict:
    account_names = {row["code"]: row["description"] for row in context.get("accounts", [])}
    current_code = movement.conta_codigo or None
    proposed_code = accepted["account_code"] if accepted else None
    effective_code = proposed_code or current_code
    issue_types = [item.tipo for item in issues]
    if movement.confianca == Decimal("1") and current_code:
        accounting_status = "CLASSIFICACAO_DETERMINISTICA_MANTIDA"
    elif accepted is not None and proposed_code:
        accounting_status = "PROPOSTA_IA_PRONTA_PARA_APROVACAO"
    else:
        accounting_status = "SEM_PROPOSTA_VALIDA_REQUER_DEFINICAO_HUMANA"
    team_premise = (
        effective_code == "1.1.09.002"
        or (effective_code == "2.1.01.001" and movement.valor > 0)
        or (effective_code == "1.1.09.001" and "TRANSFERENCIA_EM_TRANSITO" in issue_types)
    )
    authority_basis = (
        "DECISAO_FUNDAMENTADA_DO_TIME_EM_HIPOTESES" if team_premise
        else "CRITERIO_CONFIRMADO_PELO_FINANCEIRO" if effective_code
        else "INDEFINIDA_AGUARDANDO_SUPORTE"
    )
    authority_note = (
        "O fato econômico e a pendência operacional constam nos registros, mas o código contábil "
        "específico provém de decisão fundamentada do time documentada em HIPOTESES.md."
        if team_premise else
        "Critério contábil alinhado às confirmações gerais do financeiro e ao plano de contas Fênix ERP."
        if effective_code else
        "Sem conta analítica confirmada; exige definição humana com suporte documental."
    )
    valid_rca = (
        investigation["response"]
        if investigation and not investigation["validation_errors"] and isinstance(investigation.get("response"), dict)
        else None
    )
    cited_ids: list[str] = []
    if accepted:
        cited_ids.extend(accepted.get("evidence_ids", []))
    if valid_rca:
        cited_ids.extend(valid_rca.get("evidence_ids", []))
    evidences = _resolve_evidences(context, cited_ids)
    actions = [
        _ISSUE_ACTIONS[kind] for kind in dict.fromkeys(issue_types) if kind in _ISSUE_ACTIONS
    ]
    if not actions:
        actions.append("Revisar evidências e confirmar tratamento contábil e documental com o Financeiro.")
    approval_cmd = (
        f"uv run --locked --project backend python -m src.normalizar_bancos aprovar "
        f"--referencias {output_dir / 'referencias_bancarias.json'} "
        f"--saida {output_dir / 'referencias_bancarias_revisadas.json'} "
        f"--revisor \"<NOME_REVISOR>\" --movimento {identifier}"
        if accepted is not None and proposed_code else None
    )
    who = sorted({item.quem_decide for item in issues}) or ["Financeiro"]
    return {
        "movement_id": identifier,
        "status": "ESCALAR_HUMANO",
        "quem_decide": who,
        "movimento": {
            "movement_id": identifier,
            "banco": movement.banco,
            "ref_origem": movement.ref_origem,
            "data": movement.data,
            "descricao": movement.descricao,
            "valor_brl": format(movement.valor, "f"),
            "arquivo_origem": context["movement"]["source"]["file"],
            "linha_origem": context["movement"]["source"]["row"],
        },
        "classificacao_contabil": {
            "conta_codigo_atual": current_code,
            "conta_descricao_atual": account_names.get(current_code, "") if current_code else "",
            "metodo_atual": movement.metodo,
            "confianca_atual": format(movement.confianca, "f"),
            "conta_codigo_proposta": proposed_code,
            "conta_descricao_proposta": account_names.get(proposed_code, "") if proposed_code else "",
            "etapa_proposta": accepted_stage,
            "status_revisao_contabil": accounting_status,
            "base_autoridade_conta": authority_basis,
            "nota_autoridade": authority_note,
            "efeito_aprovacao": (
                "Aprovar aplica conta_codigo, metodo=IA e confianca=0.80 no replay determinístico, "
                "preservando valor, data, id_saida e mantendo as pendências operacionais abertas."
            ),
            "comando_aprovacao": approval_cmd,
        },
        "diagnostico_ia": valid_rca["diagnosis"] if valid_rca else None,
        "hipoteses_nao_confirmadas": valid_rca["hypotheses"] if valid_rca else [],
        "acoes_revisor": actions,
        "evidencias_citadas": evidences,
        "issues": [asdict(item) for item in issues],
        "missing_information": missing,
        "validation_errors": errors,
        "investigation": investigation,
        "proposal_available": accepted is not None,
    }

def generate_bank_reference(dataset: Dataset, data_dir: Path, docs_dir: Path, output_dir: Path,
    period: date, interpreter) -> dict:
    _outside_sources(output_dir, data_dir, docs_dir)
    cases = _cases(dataset, period)
    hashes = input_manifest(data_dir, docs_dir)
    chunks = document_chunks(docs_dir)
    records, investigations, escalations = [], [], []
    template_cache: dict[str, tuple[dict, str]] = {}
    for identifier, (movement, issues) in cases.items():
        accepted, accepted_stage = None, None
        attempts, errors = [], []
        investigation = None
        tkey = _template_key(movement)
        if tkey and tkey in template_cache and not any(
            item.tipo not in {"CLASSIFICACAO_BANCARIA", "CONTA_NAO_LANCAVEL", "VALIDAR_RECONHECIMENTO_CONTABIL"}
            for item in issues
        ):
            template_proposal, template_mov_id = template_cache[tkey]
            context = bank_context(dataset, movement, issues, chunks, rca=False)
            allowed = _evidence(context)
            evidences = [
                context["movement"]["source_id"] if ev == template_mov_id else ev
                for ev in template_proposal["evidence_ids"]
                if ev == template_mov_id or ev in allowed
            ]
            focal_match = next((
                row["source_id"] for row in context.get("related_records", [])
                if row.get("match_basis") == "EXACT_CNPJ"
            ), None)
            if focal_match and focal_match not in evidences:
                evidences.append(focal_match)
            adapted = BankProposal(
                movement_id=identifier,
                account_code=template_proposal["account_code"],
                evidence_ids=list(dict.fromkeys(evidences)),
                reason=template_proposal["reason"],
                missing_information=template_proposal["missing_information"],
            )
            accepted, accepted_stage = adapted.model_dump(), "CLASSIFICACAO_BANCARIA"
            attempts.append({
                "stage": "CLASSIFICACAO_BANCARIA", "context": context,
                "response": accepted, "validation_errors": [],
            })
        else:
            stages = ["RCA_BANCARIA"] if movement.confianca == Decimal("1") else ["CLASSIFICACAO_BANCARIA"]
            for stage in stages:
                context = bank_context(dataset, movement, issues, chunks, rca=stage == "RCA_BANCARIA")
                response, stage_errors = None, []
                try:
                    result = interpreter.invoke(context, stage)
                    schema = BankProposal if stage == "CLASSIFICACAO_BANCARIA" else BankInvestigation
                    if not isinstance(result, schema):
                        raise ValueError("Saída do interpretador não corresponde ao schema bancário solicitado.")
                    response = result.model_dump()
                    result = schema.model_validate(response)
                    if isinstance(result, BankInvestigation):
                        if (not result.diagnosis.strip() or not result.evidence_ids
                            or context["movement"]["source_id"] not in result.evidence_ids
                            or not set(result.evidence_ids) <= _evidence(context)):
                            raise ValueError("Diagnóstico RCA sem evidências válidas do movimento focal.")
                        proposal = result.proposal
                    else:
                        proposal = result
                    if proposal is not None:
                        _validate(proposal, context, movement, dataset)
                        if proposal.account_code is not None:
                            accepted, accepted_stage = proposal.model_dump(), stage
                except ValueError as error:
                    stage_errors.append(str(error))
                attempt = {"stage": stage, "context": context, "response": response,
                    "validation_errors": stage_errors}
                attempts.append(attempt)
                errors.extend(stage_errors)
                if stage == "CLASSIFICACAO_BANCARIA":
                    if (accepted is None or stage_errors or accepted["missing_information"].strip()
                        or any(item.tipo not in {"CLASSIFICACAO_BANCARIA", "CONTA_NAO_LANCAVEL", "VALIDAR_RECONHECIMENTO_CONTABIL"} for item in issues)):
                        stages.append("RCA_BANCARIA")
                    elif tkey and accepted is not None:
                        template_cache[tkey] = (accepted, context["movement"]["source_id"])
                else:
                    investigation = attempt
                    investigations.append({"movement_id": identifier, **attempt})
        missing = [item.motivo for item in issues if item.tipo not in {
            "CLASSIFICACAO_BANCARIA", "CONTA_NAO_LANCAVEL", "VALIDAR_RECONHECIMENTO_CONTABIL"}]
        if accepted and accepted["missing_information"].strip():
            missing.append(accepted["missing_information"])
        if investigation and investigation["response"]:
            missing.append(investigation["response"].get("missing_information", ""))
        missing = list(dict.fromkeys(item for item in missing if item.strip()))
        if accepted is None or errors or missing:
            final_context = investigation["context"] if investigation else attempts[-1]["context"]
            escalations.append(_build_escalation(
                identifier, movement, issues, final_context, accepted, accepted_stage,
                investigation, missing, errors, output_dir,
            ))
        records.append({"movement_id": identifier, "status": "A_REVISAR", "proposal": accepted,
            "proposal_hash": _hash(accepted) if accepted else None, "proposal_stage": accepted_stage,
            "attempts": attempts, "validation_errors": errors, "investigation": investigation})
    if hashes != input_manifest(data_dir, docs_dir):
        raise ValueError("Fontes alteradas durante a geração; gere novamente.")
    report = {"kind": "BANK_PROPOSALS", "version": 1, "period": f"{period.year:04d}-{period.month:02d}",
        "model": interpreter.model, "input_hashes": hashes, "dataset_hash": _hash(asdict(dataset)),
        "cases": records, "summary": {"cases": len(records),
            "proposals": sum(item["proposal"] is not None for item in records),
            "rca_cases": len(investigations), "human_cases": len(escalations)}}
    output_dir.mkdir(parents=True, exist_ok=True)
    reference = output_dir / "referencias_bancarias.json"
    if reference.exists() and _read(reference).get("kind") == "BANK_REVIEWED":
        raise ValueError("Não é permitido sobrescrever uma referência revisada.")
    write_json(reference, report)
    write_json(output_dir / "rca_bancaria.json", investigations)
    write_json(output_dir / "escaladas_humanas.json", escalations)
    with (output_dir / "classificacoes_sugeridas.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = ["movement_id", "banco", "ref_origem", "data", "descricao", "valor",
            "conta_codigo_atual", "conta_codigo_proposta", "status", "reason", "evidence_ids", "missing_information"]
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";", lineterminator="\n")
        writer.writeheader()
        for record in records:
            proposal = record["proposal"]
            if proposal is not None:
                movement = cases[record["movement_id"]][0]
                writer.writerow({"movement_id": movement.id_saida, "banco": movement.banco,
                    "ref_origem": movement.ref_origem, "data": movement.data, "descricao": movement.descricao,
                    "valor": format(movement.valor, "f"), "conta_codigo_atual": movement.conta_codigo,
                    "conta_codigo_proposta": proposal["account_code"], "status": "A_REVISAR",
                    "reason": proposal["reason"], "evidence_ids": json_text(proposal["evidence_ids"]),
                    "missing_information": proposal["missing_information"]})
    with (output_dir / "revisao_humana.csv").open("w", encoding="utf-8", newline="") as stream:
        review_fields = [
            "movement_id", "banco", "ref_origem", "data", "descricao", "valor_brl",
            "conta_codigo_atual", "conta_descricao_atual", "conta_codigo_proposta", "conta_descricao_proposta",
            "status_revisao_contabil", "base_autoridade_conta", "quem_decide", "tipos_pendencia",
            "diagnostico_ia", "hipoteses_nao_confirmadas", "informacao_faltante",
            "acoes_revisor", "evidencias_citadas", "comando_aprovacao",
        ]
        writer = csv.DictWriter(stream, fieldnames=review_fields, delimiter=";", lineterminator="\n")
        writer.writeheader()
        for item in escalations:
            mov = item["movimento"]
            cls = item["classificacao_contabil"]
            writer.writerow({
                "movement_id": item["movement_id"],
                "banco": mov["banco"],
                "ref_origem": mov["ref_origem"],
                "data": mov["data"],
                "descricao": mov["descricao"],
                "valor_brl": mov["valor_brl"],
                "conta_codigo_atual": cls["conta_codigo_atual"] or "",
                "conta_descricao_atual": cls["conta_descricao_atual"],
                "conta_codigo_proposta": cls["conta_codigo_proposta"] or "",
                "conta_descricao_proposta": cls["conta_descricao_proposta"],
                "status_revisao_contabil": cls["status_revisao_contabil"],
                "base_autoridade_conta": cls["base_autoridade_conta"],
                "quem_decide": " / ".join(item["quem_decide"]),
                "tipos_pendencia": " | ".join(issue["tipo"] for issue in item["issues"]),
                "diagnostico_ia": item["diagnostico_ia"] or "",
                "hipoteses_nao_confirmadas": " | ".join(item["hipoteses_nao_confirmadas"]),
                "informacao_faltante": " | ".join(item["missing_information"]),
                "acoes_revisor": " | ".join(item["acoes_revisor"]),
                "evidencias_citadas": json_text([
                    {"source_id": ev["source_id"], "autoridade": ev["autoridade"], "resumo": ev["resumo"]}
                    for ev in item["evidencias_citadas"]
                ]),
                "comando_aprovacao": cls["comando_aprovacao"] or "",
            })
    return report


def _reference_state(report: dict, dataset: Dataset, data_dir: Path, docs_dir: Path, period: date) -> tuple:
    if report.get("kind") != "BANK_PROPOSALS" or type(report.get("version")) is not int or report["version"] != 1:
        raise ValueError("Referência de propostas bancárias inválida ou versão não suportada.")
    if _period(report.get("period")) != period:
        raise ValueError("Referência de outra competência.")
    if report.get("input_hashes") != input_manifest(data_dir, docs_dir) or report.get("dataset_hash") != _hash(asdict(dataset)):
        raise ValueError("Referência obsoleta: hashes das fontes financeiras/documentais divergentes.")
    cases = _cases(dataset, period)
    records = report.get("cases")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError("Casos bancários inválidos.")
    indexed = {}
    for item in records:
        identifier = item.get("movement_id")
        if not isinstance(identifier, str) or identifier in indexed or identifier not in cases:
            raise ValueError("Referência contém movimento duplicado, desconhecido ou inelegível.")
        if item.get("status") != "A_REVISAR":
            raise ValueError("Proposta sem estado explícito de revisão pendente.")
        indexed[identifier] = item
    return cases, indexed, document_chunks(docs_dir)


def _reviewable(record: dict, dataset: Dataset, case: tuple, chunks: list[dict]) -> BankProposal:
    movement, issues = case
    if record.get("proposal_stage") not in {"CLASSIFICACAO_BANCARIA", "RCA_BANCARIA"}:
        raise ValueError("Etapa de origem da proposta inválida.")
    proposal = BankProposal.model_validate(record.get("proposal"))
    if record.get("proposal_hash") != _hash(proposal.model_dump()):
        raise ValueError("Hash da proposta divergente; proposta alterada.")
    context = bank_context(dataset, movement, issues, chunks, rca=record["proposal_stage"] == "RCA_BANCARIA")
    _validate(proposal, context, movement, dataset)
    if proposal.account_code is None:
        raise ValueError("Não há conta válida para aprovar.")
    return proposal


def approve_bank_reference(dataset: Dataset, data_dir: Path, docs_dir: Path, reference_file: Path,
    output_file: Path, reviewer: str, movement_ids: list[str]) -> dict:
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Informe o nome declarado do revisor.")
    if not movement_ids or len(set(movement_ids)) != len(movement_ids):
        raise ValueError("Selecione movimentos não repetidos para revisão explícita.")
    _outside_sources(output_file, data_dir, docs_dir)
    if output_file.resolve() == reference_file.resolve() or output_file.exists():
        raise ValueError("Informe um novo caminho: a aprovação não pode sobrescrever arquivos existentes.")
    report = _read(reference_file)
    period = _period(report.get("period"))
    cases, records, chunks = _reference_state(report, dataset, data_dir, docs_dir, period)
    decisions = []
    for identifier in movement_ids:
        if identifier not in records:
            raise ValueError("Movimento selecionado não consta nas propostas elegíveis.")
        record = records[identifier]
        proposal = _reviewable(record, dataset, cases[identifier], chunks)
        decisions.append({"movement_id": identifier, "proposal": proposal.model_dump(),
            "proposal_hash": record["proposal_hash"], "proposal_stage": record["proposal_stage"],
            "reviewed_by": reviewer.strip(), "status": "APROVADO"})
    if report["input_hashes"] != input_manifest(data_dir, docs_dir):
        raise ValueError("Fontes alteradas durante a revisão; gere novamente.")
    reviewed = {"kind": "BANK_REVIEWED", "version": 1, "period": report["period"],
        "input_hashes": report["input_hashes"], "dataset_hash": report["dataset_hash"],
        "reference": report, "reference_hash": _hash(report), "reviewed_by": reviewer.strip(),
        "reviewed_at": datetime.now(timezone.utc).isoformat(), "decisions": decisions,
        "summary": {"approved": len(decisions)}}
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("x", encoding="utf-8") as stream:
        stream.write(json_text(reviewed) + "\n")
    return reviewed


def load_bank_reference(dataset: Dataset, data_dir: Path, docs_dir: Path, reference_file: Path,
    period: date) -> dict[str, ReviewedBankDecision]:
    reviewed = _read(reference_file)
    if reviewed.get("kind") != "BANK_REVIEWED" or type(reviewed.get("version")) is not int or reviewed["version"] != 1:
        raise ValueError("Fechamento exige referência bancária explicitamente revisada.")
    reviewer = reviewed.get("reviewed_by")
    if not isinstance(reviewer, str) or not reviewer.strip() or not reviewed.get("reviewed_at"):
        raise ValueError("Referência sem revisão explícita identificada.")
    report = reviewed.get("reference")
    if not isinstance(report, dict) or reviewed.get("reference_hash") != _hash(report):
        raise ValueError("Hash da referência original divergente.")
    for field in ("period", "input_hashes", "dataset_hash"):
        if reviewed.get(field) != report.get(field):
            raise ValueError("Metadados revisados divergem da referência original.")
    cases, records, chunks = _reference_state(report, dataset, data_dir, docs_dir, period)
    decisions = reviewed.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("Referência revisada sem decisões aprovadas.")
    result = {}
    for item in decisions:
        if not isinstance(item, dict):
            raise ValueError("Decisão revisada inválida.")
        identifier = item.get("movement_id")
        if not isinstance(identifier, str) or identifier in result or identifier not in records:
            raise ValueError("Movimento revisado duplicado ou desconhecido.")
        record = records[identifier]
        if item.get("status") != "APROVADO" or item.get("reviewed_by") != reviewer:
            raise ValueError("Decisão sem aprovação explícita do revisor.")
        for field in ("proposal", "proposal_hash", "proposal_stage"):
            if item.get(field) != record.get(field):
                raise ValueError("Proposta revisada difere da proposta original.")
        proposal = _reviewable(record, dataset, cases[identifier], chunks)
        result[identifier] = ReviewedBankDecision(identifier, proposal.account_code, proposal.reason,
            tuple(proposal.evidence_ids), reviewer, reviewed["reference_hash"])
    return result
