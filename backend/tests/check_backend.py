from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from check_banking import check as check_banking
from check_checkout import check as check_checkout
from check_remittances import check as check_remittances

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
        response = client.get("/api/v1/diagnostico", params={"competencia": "2026-09"})
        assert response.status_code == 422
        assert directory not in response.text
        assert "message" in response.json()["detail"]


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
    check_checkout()
    check_remittances()
    check_banking()
