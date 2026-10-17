from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from check_banking import check as check_banking
from check_checkout import check as check_checkout
from check_remittances import check as check_remittances
from check_normalization import check as check_normalization
from check_flow_log import check as check_flow_log

from src.application.closing import TECHNICAL_ASSUMPTIONS, close_month
from src.application.diagnostics import diagnose
from src.domain.models import (
    BankEntry,
    CheckoutEntry,
    DataPackError,
    Dataset,
    RemittanceEntry,
    SourceRef,
)
from src.main import create_app
from src.infrastructure.parsing import cnpj, decimal_value, percentage
from src.infrastructure.readers import load_dataset

ROOT = Path(__file__).resolve().parents[2]


def check_diagnostics() -> None:
    period = date(2026, 9, 1)
    sale = CheckoutEntry(
        source=SourceRef("orbe.csv", "sale", 2), occurred_on=period,
        establishment_code="123", merchant="Lojista", kind="VENDA",
        modality="CREDITO A VISTA", installments=1,
        gross=Decimal("10.00"), applied_rate=Decimal("0.0269"),
        fee=Decimal("0.26"), net=Decimal("9.74"), original_ref=None, original_on=None,
    )
    reversal = replace(
        sale, source=SourceRef("orbe.csv", "reversal", 3), kind="ESTORNO",
        gross=-sale.gross, fee=-sale.fee, net=-sale.net,
        original_ref="sale", original_on=period,
    )
    remittance = RemittanceEntry(
        source=SourceRef("ponte.eml!relatorio.xlsx", "PC-1", 6, "Operações"),
        contracted_on=period, settled_on=date(2026, 10, 1),
        cnpj="00520815000100", merchant="Lojista", currency="USD",
        foreign_amount=Decimal("100"), customer_rate=Decimal("5"),
        ptax_rate=Decimal("5.0123"), brl_amount=Decimal("500"),
        spread=Decimal("-1.23"), spread_rate=Decimal("-0.002454"),
        estimated_spread=Decimal("9.00"), iof=Decimal("1.90"),
        status="APROVADO C/ RESSALVA", note=None,
    )
    repeated = replace(
        remittance, source=replace(remittance.source, row=7), note="reprocessada",
    )
    outgoing = BankEntry(
        SourceRef("aurora.csv", "L9", 9), "AURORA", period,
        "TRANSF ENTRE CONTAS P/ BCO MERIDIANO", Decimal("-100"), Decimal("-100"),
    )
    incoming = BankEntry(
        SourceRef("meridiano.ofx", "MRD1"), "MERIDIANO", period,
        "TED RECEBIDA - MESMA TITULARIDADE", Decimal("100"),
    )
    dataset = Dataset(
        checkout=(sale, reversal), contracts=(), remittances=(remittance, repeated),
        banks=(outgoing, incoming), accounts=(),
        bank_opening_balances={"AURORA": Decimal("0")},
        bank_closing_balances={"AURORA": Decimal("-100"), "MERIDIANO": Decimal("100")},
    )
    report = diagnose(dataset, period)
    assert report.checkout.reported_fees == Decimal("0.00")
    assert report.checkout.fees_matching_truncation == 2
    assert {group.kind: group.fees for group in report.checkout.by_kind} == {
        "ESTORNO": Decimal("-0.26"), "VENDA": Decimal("0.26"),
    }
    assert report.remittances.rows == 2 and report.remittances.unique_operations == 1
    assert report.remittances.reported_spread == Decimal("-2.46")
    assert report.remittances.negative_spread == Decimal("-2.46")
    assert report.remittances.status_counts == {"APROVADO C/ RESSALVA": 2}
    assert report.remittances.settlements_outside_period == 2
    findings = {finding.code: finding for finding in report.findings}
    assert "REPROCESSED_REMITTANCE_ID" in findings
    assert "CONFLICTING_REMITTANCE_ID" not in findings
    assert "ORIGINAL_OUTSIDE_PACK" not in findings
    assert "OWN_TRANSFER_UNRECONCILED" not in findings
    assert findings["UNMATCHED_ESTABLISHMENT"].sources == (sale.source, reversal.source)
    assert {bank.bank: bank.net for bank in report.banks} == {
        "AURORA": Decimal("-100"), "MERIDIANO": Decimal("100"),
    }

    conflict = replace(repeated, spread=Decimal("-2.00"))
    changed = diagnose(replace(dataset, remittances=(remittance, conflict)), period)
    assert any(f.code == "CONFLICTING_REMITTANCE_ID" and f.severity == "ERROR" for f in changed.findings)
    assert changed.remittances.reported_spread == Decimal("-3.23")

    extra_credit = replace(incoming, source=SourceRef("meridiano.ofx", "MRD2"))
    ambiguous = diagnose(replace(dataset, banks=(outgoing, incoming, extra_credit)), period)
    unpaired = [f for f in ambiguous.findings if f.code == "OWN_TRANSFER_UNRECONCILED"]
    assert len(unpaired) == 1 and len(unpaired[0].sources) == 3

    orphan = replace(reversal, original_ref="missing")
    incomplete = diagnose(replace(dataset, checkout=(orphan,)), period)
    assert any(f.code == "ORIGINAL_OUTSIDE_PACK" and f.sources == (orphan.source,) for f in incomplete.findings)
    assert incomplete.checkout.reported_fees == Decimal("-0.26")

    broken = replace(outgoing, balance=Decimal("-99"))
    wrong_balance = diagnose(replace(dataset, banks=(broken, incoming)), period)
    assert any(f.code == "BANK_RUNNING_BALANCE_MISMATCH" for f in wrong_balance.findings)

    october = diagnose(dataset, date(2026, 10, 1))
    assert october.checkout.rows == 0 and october.remittances.rows == 0
    assert october.banks == ()
    assert any(f.code == "NO_ROWS_IN_PERIOD" for f in october.findings)
    ancient = diagnose(dataset, date(1, 9, 1))
    assert ancient.period == "0001-09"


def check_http_boundaries() -> None:
    with TemporaryDirectory() as directory:
        client = TestClient(create_app(Path(directory)))
        for invalid in ("2026-13", "2026-00", "0000-09", "2026-9", "../dados_tarvo"):
            response = client.get("/api/v1/diagnostico", params={"competencia": invalid})
            assert response.status_code == 422, (invalid, response.text)
        for route in ("/api/v1/diagnostico", "/api/v1/fechamento"):
            response = client.get(route, params={"competencia": "2026-09"})
            assert response.status_code == 422
            assert directory not in response.text
            assert "Fonte de dados não permitida" in response.json()["detail"]["message"]
    for outside in (ROOT / "docs", ROOT):
        response = TestClient(create_app(outside)).get("/api/v1/fechamento", params={"competencia": "2026-09"})
        assert response.status_code == 422 and "dados_tarvo" in response.json()["detail"]["message"]
    response = TestClient(create_app(ROOT / "dados_tarvo")).get("/api/v1/fechamento", params={"competencia": "2026-09"})
    assert response.status_code == 200 and response.json()["status"] == "PRELIMINAR_COM_PENDENCIAS"
    assert response.json()["premissas_tecnicas"]


def check_real_closing() -> None:
    """Fechamento de set/2026 sem referência normalizada: somente dados_tarvo, sem vínculos externos."""
    dataset = load_dataset(ROOT / "dados_tarvo")
    result = close_month(dataset, date(2026, 9, 1))
    assert result.competencia == "2026-09" and result.status == "PRELIMINAR_COM_PENDENCIAS"
    assert result.premissas_tecnicas == TECHNICAL_ASSUMPTIONS and "dados_tarvo" in result.premissas_tecnicas[0]

    def total(rows, field="valor_brl") -> Decimal:
        return sum((getattr(row, field) for row in rows), Decimal("0"))

    checkout = [row for row in result.comissoes_por_lojista if row.linha_negocio == "CHECKOUT"]
    remittance = [row for row in result.comissoes_por_lojista if row.linha_negocio == "REMESSA"]
    assert (len(checkout), total(checkout, "comissao_brl")) == (18, Decimal("194943.54"))
    assert (len(remittance), total(remittance, "comissao_brl")) == (14, Decimal("38626.42"))
    by_line = Counter(row.linha_negocio for row in result.divergencias_taxa)
    assert by_line == {"CHECKOUT": 92, "REMESSA": 26}
    assert total([r for r in result.divergencias_taxa if r.linha_negocio == "CHECKOUT"], "diferenca_brl") == Decimal("1576.23")
    remittance_divergences = [r for r in result.divergencias_taxa if r.linha_negocio == "REMESSA"]
    assert total(remittance_divergences, "diferenca_brl") == Decimal("205.86")
    mundo = "17217888000111"
    assert Counter(row.cnpj for row in remittance_divergences)[mundo] == 23
    assert all(row.taxa_contratada_pct == Decimal("1.200") for row in remittance_divergences if row.cnpj == mundo)

    pending = Counter(row.tipo for row in result.pendencias)
    assert pending == {
        "CHECKOUT_BLOQUEADO": 290, "CLASSIFICACAO_BANCARIA": 109, "DIVERGENCIA_CHECKOUT": 92,
        "VALIDAR_RECONHECIMENTO_CONTABIL": 61, "CONTRATO_REMESSA_NAO_RESOLVIDO": 53,
        "VALIDAR_DIVERGENCIA_REMESSA": 26, "VALIDAR_SPREAD_NEGATIVO": 4, "VALIDAR_POLITICA_APURACAO": 2,
        "CONDICAO_COMERCIAL_NAO_DEFINIDA": 1, "CORRIGIR_RELATORIO_PONTE": 1, "IDENTIFICAR_PIX": 1,
        "CONCILIAR_TRANSFERENCIA_PROPRIA": 1,
    }
    assert not {"CONTESTAR_REMESSA_PONTE", "CONFERIR_REPASSE_FUTURO_PONTE", "SALDO_NEGATIVO_CHECKOUT"} & set(pending)
    assert not any("Helena" in row.quem_decide for row in result.pendencias)
    ec_of = {entry.source.ref: entry.establishment_code for entry in dataset.checkout}
    blocked = [row for row in result.pendencias if row.tipo == "CHECKOUT_BLOQUEADO"]
    assert Counter(ec_of[row.ref_origem] for row in blocked) == {
        "1038867654": 85, "1043396662": 75, "1043300981": 73, "1076801732": 57,
    }
    assert total(blocked) == Decimal("35368.67") and {row.quem_decide for row in blocked} == {"Comercial"}
    unbound = {"78576234000179", "07752116000126", "66564512000199"}
    assert not unbound & {row.cnpj for row in checkout}

    casa = "00520815000100"
    casa_checkout = next(row for row in checkout if row.cnpj == casa)
    assert casa_checkout.comissao_brl == Decimal("15391.92")
    condition = next(row for row in result.pendencias if row.tipo == "CONDICAO_COMERCIAL_NAO_DEFINIDA")
    assert condition.id_saida == f"condicao-comercial:2026-09:{casa}" and condition.valor_brl is None
    invoices = result.notas_a_emitir
    assert len(invoices) == 18 and casa not in {row.tomador_cnpj for row in invoices if row.linha_negocio == "CHECKOUT"}
    assert total([row for row in invoices if row.linha_negocio == "CHECKOUT"]) == Decimal("194943.54") - casa_checkout.comissao_brl
    remittance_invoice = [row for row in invoices if row.linha_negocio == "REMESSA"]
    assert [(row.tomador_cnpj, row.valor_brl) for row in remittance_invoice] == [("75374179000146", Decimal("38626.42"))]

    negatives = {row.ref_origem: row.valor_brl for row in result.pendencias if row.tipo == "VALIDAR_SPREAD_NEGATIVO"}
    assert negatives == {"PC-0926-0210": Decimal("-687.52"), "PC-0926-0326": Decimal("-509.22"),
        "PC-0926-0491": Decimal("-292.28"), "PC-0926-0545": Decimal("-345.03")}
    negative_traces = {row.ref_origem: row.valor for row in result.rastro
        if row.id_saida.startswith("REMESSA_COMISSAO") and row.ref_origem in negatives}
    assert negative_traces == {"PC-0926-0210": Decimal("-343.76"), "PC-0926-0326": Decimal("-254.61"),
        "PC-0926-0491": Decimal("-146.14"), "PC-0926-0545": Decimal("-172.52")}
    duplicate = next(row for row in result.pendencias if row.tipo == "CORRIGIR_RELATORIO_PONTE")
    assert (duplicate.ref_origem, duplicate.valor_brl) == ("PC-0926-0162", Decimal("821.88"))
    unclassified = {row.ref_origem for row in result.pendencias if row.tipo == "CLASSIFICACAO_BANCARIA"}
    assert {"L18", "L65", "MRD2026090425"} <= unclassified
    assert len(result.lancamentos_classificados) == 184
    assert Counter(row.conta_codigo for row in result.lancamentos_classificados)[""] == 109
    assert {row.quem_decide for row in result.pendencias if row.tipo in {
        "CLASSIFICACAO_BANCARIA", "VALIDAR_RECONHECIMENTO_CONTABIL", "IDENTIFICAR_PIX",
        "CONCILIAR_TRANSFERENCIA_PROPRIA", "VALIDAR_POLITICA_APURACAO"}} == {"Financeiro"}


def check_financial_input() -> None:
    source = SourceRef("entrada.csv", "linha")
    assert decimal_value("-1.234,56", source, "valor", ptbr=True) == Decimal("-1234.56")
    assert percentage("2,69%", source, "taxa") == percentage(0.0269, source, "taxa")
    assert percentage("2,69", source, "taxa", percentage_points=True) == Decimal("0.0269")
    assert cnpj(520815000100, source) == "00520815000100"
    for invalid in ("NaN", "Infinity", "1.2.3", "", True, float("nan")):
        try:
            decimal_value(invalid, source, "valor")
        except DataPackError as error:
            assert error.source == source
        else:
            raise AssertionError(f"Entrada financeira inválida aceita: {invalid!r}")


if __name__ == "__main__":
    check_diagnostics()
    check_http_boundaries()
    check_financial_input()
    check_real_closing()
    check_checkout()
    check_remittances()
    check_banking()
    check_normalization()
    check_flow_log()
