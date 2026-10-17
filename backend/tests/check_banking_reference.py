import csv
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError
from src.application.banking import classify_banks
from src.application.banking_reference import (
    _hash, approve_bank_reference, generate_bank_reference, load_bank_proposals, load_bank_reference,
)
from src.domain.banking_reference import BankInvestigation, BankProposal, ReviewedBankDecision
from src.domain.models import Account, BankEntry, Dataset, RemittanceEntry, SourceRef
from src.infrastructure.banking_sources import bank_context, markdown_chunks as document_chunks
from src.infrastructure.normalization_sources import json_text
from src.infrastructure.openai_normalizer import _bank_text_format
def _reject(call) -> None:
    try:
        call()
    except ValueError:
        pass
    else:
        raise AssertionError("Referência bancária insegura foi aceita.")


class Interpreter:
    model = "offline-assertions"

    def __init__(self):
        self.calls = []

    def invoke(self, context, stage):
        self.calls.append((context, stage))
        movement = context["movement"]
        if stage == "RCA_BANCARIA":
            return BankInvestigation(diagnosis="Extrato não esclarece a finalidade nem substitui comprovantes.",
                hypotheses=["Identificação depende do financeiro."], evidence_ids=[movement["source_id"]],
                proposal=None, missing_information="Solicitar comprovante e confirmação de Helena.")
        account = next(row for row in context["accounts"] if row["code"] == "1.1.09.002")
        return BankProposal(movement_id=movement["movement_id"], account_code=account["code"],
            evidence_ids=[movement["source_id"], account["source_id"]],
            reason="Manter crédito a identificar em conta transitória, sem reconhecer receita.",
            missing_information="Identificar pagador e finalidade antes de conciliar.")


def check() -> None:
    period = date(2026, 9, 1)
    codes = ("1.1.09.002", "2.1.01.001", "1.1.09.001", "1.1.02.001", "4.2.01")
    accounts = tuple(Account(SourceRef("contas.csv", code, index + 2), code,
        "Conta " + code, "ANALITICA", "D", True) for index, code in enumerate(codes))
    entries = tuple(BankEntry(SourceRef("aurora.csv", ref, int(ref[1:])), "AURORA",
        date(2026, 9, 30), description, Decimal(amount)) for ref, description, amount in (
            ("L10", "CREDITO PENDENTE DE CLASSIFICACAO", "125.43"),
            ("L50", "PIX RECEB SEM IDENTIFICACAO CNPJ 12345678000190", "987.65"),
            ("L65", "DEVOLUCAO PIX MUNDO ABERTO", "200.00"),
            ("L92", "TRANSF ENTRE CONTAS", "-300000.00"),
            ("L99", "APLICACAO AUTOMATICA CDB", "-30.00")))
    entries += (BankEntry(SourceRef("meridiano.ofx", "L50"), "MERIDIANO",
        date(2026, 9, 30), "RESGATE AUTOMATICO CDB", Decimal("15")),)
    accounts += (Account(SourceRef("contas.csv", "2.1.02.001", 9), "2.1.02.001",
        "Recursos de clientes", "ANALITICA", "C", True),)
    accounts += (
        Account(SourceRef("contas.csv", "4", 10), "4", "Despesas", "SINTETICA", "D", False),
        Account(SourceRef("contas.csv", "4.2.99", 11), "4.2.99", "Bloqueada", "ANALITICA", "D", False),
    )
    dataset = Dataset((), (), (), entries, accounts, {}, {})
    original = asdict(dataset)
    baseline = classify_banks(dataset, period)
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        data, docs, output = root / "dados", root / "docs", root / "sugestoes"
        data.mkdir()
        docs.mkdir()
        financial = data / "originais.csv"
        financial.write_text("fontes financeiras imutáveis\n", encoding="utf-8")
        document = docs / "THREADS_FINANCEIRO_TARVO.md"
        document.write_text("Helena: PIX SEM IDENTIFICACAO deve permanecer pendente.\n"
            "MUNDO ABERTO: investigar motivo da devolução.\n"
            "Transferência de 30/09: conferir extrato de outubro.\n", encoding="utf-8")
        interpreter = Interpreter()
        report = generate_bank_reference(dataset, data, docs, output, period, interpreter)
        proposal_file = output / "referencias_bancarias.json"
        approved_file = root / "revisada.json"
        assert asdict(dataset) == original
        assert report["kind"] == "BANK_PROPOSALS" and report["summary"]["cases"] == 4
        for context, stage in interpreter.calls:
            if context["baseline"]["confianca"] == Decimal("1"):
                assert stage == "RCA_BANCARIA"
        assert {context["movement"]["ref"] for context, _ in interpreter.calls} == {"L10", "L50", "L65", "L92"}
        assert all(record["status"] == "A_REVISAR" for record in report["cases"])
        assert all((output / name).exists() for name in (
            "classificacoes_sugeridas.csv", "rca_bancaria.json", "escaladas_humanas.json", "revisao_humana.csv"))
        suggestions = [record for record in report["cases"] if record["proposal"]]
        assert len(suggestions) == 3
        assert all(record["proposal_stage"] == "CLASSIFICACAO_BANCARIA" for record in suggestions)
        assert all(record["investigation"]["response"]["proposal"] is None for record in suggestions)
        escalations = json.loads((output / "escaladas_humanas.json").read_text(encoding="utf-8"))
        assert len(escalations) == 4
        assert all("Solicitar comprovante e confirmação de Helena." in row["missing_information"] for row in escalations)
        by_ref = {row["movimento"]["ref_origem"]: row for row in escalations}
        assert by_ref["L50"]["classificacao_contabil"]["status_revisao_contabil"] == "PROPOSTA_IA_PRONTA_PARA_APROVACAO"
        assert by_ref["L50"]["classificacao_contabil"]["base_autoridade_conta"] == "DECISAO_FUNDAMENTADA_DO_TIME_EM_HIPOTESES"
        assert by_ref["L50"]["classificacao_contabil"]["comando_aprovacao"]
        assert by_ref["L92"]["classificacao_contabil"]["status_revisao_contabil"] == "CLASSIFICACAO_DETERMINISTICA_MANTIDA"
        assert by_ref["L92"]["classificacao_contabil"]["comando_aprovacao"] is None
        assert all(row["evidencias_citadas"] and row["acoes_revisor"] for row in escalations)
        with (output / "revisao_humana.csv").open(encoding="utf-8", newline="") as stream:
            review_rows = list(csv.DictReader(stream, delimiter=";"))
        assert len(review_rows) == 4 and {row["ref_origem"] for row in review_rows} == {"L10", "L50", "L65", "L92"}
        _reject(lambda: load_bank_reference(dataset, data, docs, proposal_file, period))
        selected = [record["movement_id"] for record in suggestions]
        proposed = load_bank_proposals(dataset, data, docs, proposal_file, period)
        preview = classify_banks(dataset, period, proposals=proposed)
        assert set(proposed) == set(selected)
        assert {row.id_saida for row in preview.movements if row.metodo == "IA"} == set(selected)
        assert len([row for row in preview.pending if row.tipo == "VALIDAR_CLASSIFICACAO_IA"]) == len(selected)
        assert any(row.tipo == "MOTIVO_DEVOLUCAO_PIX" and row.ref_origem == "L65" for row in preview.pending)
        assert all(after.valor == before.valor and after.ref_origem == before.ref_origem
            for before, after in zip(baseline.movements, preview.movements))
        _reject(lambda: classify_banks(dataset, period, proposals=proposed, reviewed_decisions={
            selected[0]: ReviewedBankDecision(
                selected[0], "1.1.09.002", "Conflito", ("extrato",), "Revisor", "hash")}))
        _reject(lambda: approve_bank_reference(dataset, data, docs, proposal_file, approved_file, " ", selected))
        _reject(lambda: approve_bank_reference(dataset, data, docs, proposal_file, approved_file, "Rodrigo", selected * 2))
        _reject(lambda: approve_bank_reference(dataset, data, docs, proposal_file, approved_file, "Rodrigo", ["inventado"]))
        reviewed = approve_bank_reference(dataset, data, docs, proposal_file, approved_file, "Rodrigo", selected)
        decisions = load_bank_reference(dataset, data, docs, approved_file, period)
        assert set(decisions) == set(selected) and all(decision.reviewed_by == "Rodrigo" for decision in decisions.values())
        assert all(decision.reference_id == reviewed["reference_hash"] for decision in decisions.values())
        replay = classify_banks(dataset, period, reviewed_decisions=decisions)
        for before, after in zip(baseline.movements, replay.movements):
            assert (before.id_saida, before.data, before.descricao, before.valor, before.ref_origem, before.banco) == (
                after.id_saida, after.data, after.descricao, after.valor, after.ref_origem, after.banco)
            if before.id_saida in selected:
                assert after.metodo == "IA" and after.conta_codigo == "1.1.09.002"
        assert any(item.tipo == "IDENTIFICAR_PIX" for item in replay.pending)
        assert {(trace.id_saida, trace.arquivo_origem, trace.ref_origem, trace.valor,
            trace.linha_origem, trace.aba_origem) for trace in replay.traces if trace.id_saida in selected} == {
            (trace.id_saida, trace.arquivo_origem, trace.ref_origem, trace.valor,
                trace.linha_origem, trace.aba_origem) for trace in baseline.traces if trace.id_saida in selected}
        assert asdict(dataset) == original
        _reject(lambda: approve_bank_reference(dataset, data, docs, proposal_file, approved_file, "Rodrigo", selected))
        _reject(lambda: load_bank_reference(dataset, data, docs, approved_file, date(2026, 10, 1)))
        changed_dataset = replace(dataset, banks=(replace(entries[0], amount=Decimal("999")),) + entries[1:])
        _reject(lambda: load_bank_reference(changed_dataset, data, docs, approved_file, period))
        _reject(lambda: load_bank_proposals(changed_dataset, data, docs, proposal_file, period))
        for source in (financial, document):
            previous = source.read_text(encoding="utf-8")
            source.write_text(previous + "alteração\n", encoding="utf-8")
            _reject(lambda: load_bank_reference(dataset, data, docs, approved_file, period))
            _reject(lambda: load_bank_proposals(dataset, data, docs, proposal_file, period))
            _reject(lambda: approve_bank_reference(dataset, data, docs, proposal_file, root / "obsoleta.json", "Rodrigo", selected))
            source.write_text(previous, encoding="utf-8")
        tampered_file = root / "alterada.json"
        for field, value in (
            ("account_code", "CONTA_INVENTADA"), ("account_code", "4"), ("account_code", "4.2.99"),
            ("evidence_ids", [selected[0], "nf-inventada.pdf:L1"]),
        ):
            tampered = deepcopy(report)
            record = next(item for item in tampered["cases"] if item["movement_id"] == selected[0])
            record["proposal"][field] = value
            record["proposal_hash"] = _hash(record["proposal"])
            record["attempts"][0]["context"]["documents"].append({
                "source_id": "nf-inventada.pdf:L1", "file": "nf-inventada.pdf", "text": "Contexto adulterado."})
            tampered_file.write_text(json_text(tampered), encoding="utf-8")
            _reject(lambda: approve_bank_reference(dataset, data, docs, tampered_file, root / "invalida.json", "Rodrigo", [selected[0]]))
        tampered = deepcopy(report)
        tampered["cases"].append(deepcopy(tampered["cases"][0]))
        tampered_file.write_text(json_text(tampered), encoding="utf-8")
        _reject(lambda: approve_bank_reference(dataset, data, docs, tampered_file, root / "duplicada.json", "Rodrigo", selected))
        for mutation in ("review", "hash", "proposal", "duplicate", "unknown", "reference"):
            tampered = deepcopy(reviewed)
            if mutation == "review":
                tampered["reviewed_by"] = ""
            elif mutation == "hash":
                tampered["decisions"][0]["proposal_hash"] = "0" * 64
            elif mutation == "proposal":
                tampered["decisions"][0]["proposal"]["reason"] = "Alterado após revisão."
            elif mutation == "duplicate":
                tampered["decisions"].append(deepcopy(tampered["decisions"][0]))
            elif mutation == "unknown":
                tampered["decisions"][0]["movement_id"] = "movimento-inventado"
            else:
                tampered["reference"]["model"] = "referência alterada"
            tampered_file.write_text(json_text(tampered), encoding="utf-8")
            _reject(lambda: load_bank_reference(dataset, data, docs, tampered_file, period))
        sample_context = interpreter.calls[0][0]
        proposal_schema = _bank_text_format(sample_context, "CLASSIFICACAO_BANCARIA")
        for bad_kwargs in (
            {"evidence_ids": [sample_context["movement"]["source_id"], "THREADS_FINANCEIRO_TARVO.md:L999-L1005"]},
            {"account_code": "4"},
            {"account_code": "4.2.99"},
            {"movement_id": "movimento:2026-09:AURORA:aurora.csv:OUTRO"},
        ):
            base_kwargs = {
                "movement_id": sample_context["movement"]["movement_id"],
                "account_code": "1.1.09.002",
                "evidence_ids": [sample_context["movement"]["source_id"]],
                "reason": "Teste de restrição do schema.",
                "missing_information": "",
                **bad_kwargs,
            }
            try:
                proposal_schema.model_validate(base_kwargs)
            except ValidationError:
                pass
            else:
                raise AssertionError(f"Schema dinâmico aceitou valor inválido: {bad_kwargs}")
        class Refusal(Interpreter):
            def invoke(self, context, stage):
                if stage == "RCA_BANCARIA":
                    return super().invoke(context, stage)
                self.calls.append((context, stage))
                return BankProposal(movement_id=context["movement"]["movement_id"], account_code=None,
                    evidence_ids=[context["movement"]["source_id"]],
                    reason="Sem vínculo comprovado.", missing_information="Identificar o beneficiário.")

        pix_entries = (
            replace(entries[0], description="PIX ENVIADO LOJISTA UM", amount=Decimal("-100")),
            replace(entries[2], description="PIX ENVIADO LOJISTA DOIS", amount=Decimal("-200")),
        )
        refusal = Refusal()
        refused = generate_bank_reference(replace(dataset, banks=pix_entries), data, docs,
            root / "recusas", period, refusal)
        assert refused["summary"]["cases"] == 2 and refused["summary"]["proposals"] == 0
        assert len(refusal.calls) == 4
        rem_op = RemittanceEntry(
            source=SourceRef("ponte.eml!rel.xlsx", "PC-0926-0001", 6, "Operações"),
            contracted_on=date(2026, 9, 1), settled_on=date(2026, 9, 2),
            cnpj="11111111000111", merchant="Mundo Aberto", currency="USD",
            foreign_amount=Decimal("1000"), customer_rate=Decimal("5.10"),
            ptax_rate=Decimal("5.00"), brl_amount=Decimal("5100.00"),
            spread=Decimal("100.00"), spread_rate=Decimal("0.02"),
            estimated_spread=Decimal("100.00"), iof=Decimal("19.38"),
            status="APROVADO", note=None,
        )
        dup_op = replace(rem_op, source=SourceRef("ponte.eml!rel.xlsx", "PC-0926-0001", 7, "Operações"))
        ted_entry = BankEntry(
            SourceRef("meridiano.ofx", "MRD1"), "MERIDIANO", date(2026, 9, 1),
            "TED ENVIADA PONTE CAMBIO SA LIQ OPERACOES", Decimal("-5119.38"),
        )
        ponte_dataset = replace(dataset, remittances=(rem_op, dup_op), banks=(*dataset.banks, ted_entry))
        ponte_mov = next(m for m in classify_banks(ponte_dataset, period).movements if m.ref_origem == "MRD1")
        ponte_ctx = bank_context(ponte_dataset, ponte_mov, [], document_chunks(docs))
        rem_candidates = [r for r in ponte_ctx["related_records"] if r["match_basis"] == "DAILY_REMITTANCE_CANDIDATE"]
        assert len(rem_candidates) == 1
        assert rem_candidates[0]["daily_remittance_total_brl"] == Decimal("5119.38")
        assert rem_candidates[0]["daily_aggregate_matches_ted"] is True
        assert rem_candidates[0]["duplicate_occurrences_ignored"] == 1
        conflict_op = replace(dup_op, brl_amount=Decimal("5200.00"))
        conflict_dataset = replace(dataset, remittances=(rem_op, conflict_op), banks=(*dataset.banks, ted_entry))
        conflict_ctx = bank_context(conflict_dataset, ponte_mov, [], document_chunks(docs))
        conflict_candidates = [r for r in conflict_ctx["related_records"] if r["match_basis"] == "DAILY_REMITTANCE_CANDIDATE"]
        assert conflict_candidates[0]["conflicting_occurrences"] is True
        assert conflict_candidates[0]["daily_aggregate_matches_ted"] is False
