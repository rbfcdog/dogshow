from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.application.checkout import close_checkout
from src.domain.closing import ClosingPart
from src.domain.models import CheckoutEntry, Contract, DataPackError, Dataset, SourceRef


def _dataset(entries: tuple[CheckoutEntry, ...], contracts: tuple[Contract, ...]) -> Dataset:
    return Dataset(entries, contracts, (), (), (), {}, {})


def _assert_traces(part: ClosingPart) -> None:
    outputs = {
        row.id_saida: getattr(row, field)
        for rows, field in (
            (part.commissions, "comissao_brl"),
            (part.divergences, "diferenca_brl"),
            (part.invoices, "valor_brl"),
            (part.movements, "valor"),
            (part.pending, "valor_brl"),
        )
        for row in rows
        if getattr(row, field) is not None
    }
    assert {item.id_saida for item in part.traces} == set(outputs)
    for identifier, amount in outputs.items():
        contributions = [item for item in part.traces if item.id_saida == identifier]
        assert sum((item.valor for item in contributions), Decimal(0)) == amount


def check() -> None:
    period = date(2026, 9, 1)
    contract = Contract(
        SourceRef("contratos.xlsx", "A6:L6", 6, "Taxas vigentes"),
        "Razão social contratada", "11111111000111", "123",
        {
            "DEBITO": Decimal("0.01"), "CREDITO A VISTA": Decimal("0.02"),
            "CREDITO 2-6X": Decimal("0.03"), "CREDITO 7-12X": Decimal("0.04"),
            "PIX": Decimal("0.005"), "ANTECIPACAO": Decimal("0.015"),
        },
        None, "01/01/2026", None,
    )
    sale = CheckoutEntry(
        SourceRef("orbe.csv", "sale", 2), period, "123", "Apelido da adquirente",
        "VENDA", "CREDITO A VISTA", 1, Decimal("100"), Decimal("0.02"),
        Decimal("1.99"), Decimal("98.01"), None, None,
    )
    part = close_checkout(_dataset((sale,), (contract,)), period)
    assert not part.divergences and not part.pending
    assert part.commissions[0].comissao_brl == Decimal("2.00")
    assert part.invoices[0].valor_brl == Decimal("2.00")
    _assert_traces(part)

    rounding_contract = replace(contract, rates={"CREDITO A VISTA": Decimal("0.01")})
    half = replace(sale, gross=Decimal("0.50"), applied_rate=Decimal("0.01"), fee=Decimal("0"))
    second_half = replace(half, source=SourceRef("orbe.csv", "half-2", 3))
    part = close_checkout(_dataset((half, second_half), (rounding_contract,)), period)
    assert part.commissions[0].comissao_brl == Decimal("0.02")
    assert part.invoices[0].valor_brl == Decimal("0.02")
    assert [item.valor for item in part.traces if item.id_saida == part.commissions[0].id_saida] == [
        Decimal("0.01"), Decimal("0.01"),
    ]
    _assert_traces(part)
    negative_half = replace(
        half, source=SourceRef("orbe.csv", "half-refund", 4), kind="ESTORNO",
        gross=Decimal("-0.50"), original_on=date(2026, 8, 1), original_ref="outside-pack",
    )
    part = close_checkout(_dataset((negative_half,), (rounding_contract,)), period)
    assert part.commissions[0].comissao_brl == Decimal("-0.01")
    assert not part.invoices
    assert [(row.tipo, row.valor_brl, row.quem_decide) for row in part.pending] == [
        ("SALDO_NEGATIVO_CHECKOUT", Decimal("-0.01"), "Financeiro"),
    ]
    _assert_traces(part)
    part = close_checkout(_dataset((half, negative_half), (rounding_contract,)), period)
    assert part.commissions[0].comissao_brl == Decimal("0.00")
    assert not part.invoices and not part.pending
    _assert_traces(part)

    old_contract = replace(contract, merchant="Bridge anterior", validity="até 14/09/2026")
    new_contract = replace(
        contract, merchant="Bridge vigente", validity="a partir de 15/09/2026",
        rates={**contract.rates, "CREDITO A VISTA": Decimal("0.03"), "ANTECIPACAO": Decimal("0.04")},
        source=SourceRef("contratos.xlsx", "A7:L7", 7, "Taxas vigentes"),
    )
    before = replace(sale, occurred_on=date(2026, 9, 14))
    after = replace(sale, source=SourceRef("orbe.csv", "after", 3), occurred_on=date(2026, 9, 15), applied_rate=Decimal("0.03"))
    refund = replace(
        sale, source=SourceRef("orbe.csv", "refund", 4), occurred_on=date(2026, 9, 20),
        kind="ESTORNO", gross=Decimal("-100"), modality="DEBITO", applied_rate=Decimal("0.025"),
        original_ref="sale", original_on=before.occurred_on,
    )
    part = close_checkout(_dataset((after, refund, before), (new_contract, old_contract)), period)
    assert part.commissions[0].comissao_brl == Decimal("3.00")
    assert part.commissions[0].lojista == "Bridge vigente"
    assert part.invoices[0].valor_brl == Decimal("3.00")
    assert part.divergences[0].taxa_contratada_pct == Decimal("2")
    assert part.divergences[0].taxa_aplicada_pct == Decimal("2.5")
    assert part.divergences[0].diferenca_brl == Decimal("-0.50")
    assert part.pending[0].ref_origem == "refund" and part.pending[0].valor_brl == Decimal("-0.50")
    _assert_traces(part)
    august_refund = replace(refund, original_ref="august-missing", original_on=date(2026, 8, 22), modality="CREDITO A VISTA")
    part = close_checkout(_dataset((august_refund,), (old_contract, new_contract)), period)
    assert part.commissions[0].comissao_brl == Decimal("-2.00")
    assert not any(item.tipo == "CHECKOUT_BLOQUEADO" for item in part.pending)
    _assert_traces(part)
    assert not close_checkout(_dataset((august_refund,), (old_contract, new_contract)), date(2026, 8, 1)).commissions

    advance = replace(
        before, source=SourceRef("orbe.csv", "advance", 5), kind="ANTECIPACAO",
        modality="", installments=None, gross=Decimal("10"), applied_rate=Decimal("0.015"),
    )
    reversal = replace(
        advance, source=SourceRef("orbe.csv", "reverse", 6), occurred_on=date(2026, 9, 20),
        kind="REVERSAO ANTECIPACAO", gross=Decimal("-10"), original_ref="advance", original_on=advance.occurred_on,
    )
    part = close_checkout(_dataset((advance, reversal, after), (old_contract, new_contract)), period)
    assert part.commissions[0].comissao_brl == Decimal("3.00")
    assert len(part.invoices) == 1 and part.invoices[0].valor_brl == Decimal("3.00")
    assert not part.divergences
    amounts = {item.ref_origem: item.valor for item in part.traces if item.id_saida == part.commissions[0].id_saida}
    assert amounts["advance"] == Decimal("0.15") and amounts["reverse"] == Decimal("-0.15")
    _assert_traces(part)
    for invalid in (
        replace(reversal, gross=Decimal("-5")),
        replace(reversal, original_ref="missing"),
        replace(reversal, original_on=date(2026, 9, 15)),
    ):
        part = close_checkout(_dataset((advance, invalid), (old_contract, new_contract)), period)
        assert part.commissions[0].comissao_brl == Decimal("0.15")
        assert len(part.pending) == 1 and part.pending[0].tipo == "CHECKOUT_BLOQUEADO"
        assert part.pending[0].quem_decide == "Financeiro"
        _assert_traces(part)

    # Casa do Estudante: o comentário da célula A13 só indica que a condição comercial precisa ser
    # esclarecida. Não pode virar isenção: vale a taxa da planilha, sem nota até o Comercial definir.
    casa = replace(
        contract, cnpj="00520815000100", merchant="Casa do Estudante",
        comments="conversar com Diego (comercial) sobre condição especial",
    )
    casa_sale = replace(sale, applied_rate=Decimal("0.03"))
    casa_advance = replace(advance, occurred_on=date(2026, 9, 5))
    casa_refund = replace(august_refund, applied_rate=Decimal("0.02"))
    part = close_checkout(_dataset((casa_sale, casa_advance, casa_refund), (casa,)), period)
    assert [(row.cnpj, row.comissao_brl) for row in part.commissions] == [("00520815000100", Decimal("0.15"))]
    assert not part.invoices
    assert [(row.ref_origem, row.taxa_contratada_pct, row.taxa_aplicada_pct, row.diferenca_brl)
        for row in part.divergences] == [("sale", Decimal("2.00"), Decimal("3.00"), Decimal("1.00"))]
    assert all(row.taxa_contratada_pct != 0 for row in part.divergences)
    casa_trace = {item.ref_origem: item.valor for item in part.traces if item.id_saida == part.commissions[0].id_saida}
    assert casa_trace == {"advance": Decimal("0.15"), "sale": Decimal("2.00"), "refund": Decimal("-2.00")}
    condition = [row for row in part.pending if row.tipo == "CONDICAO_COMERCIAL_NAO_DEFINIDA"]
    assert len(condition) == 1 and condition[0].valor_brl is None and condition[0].quem_decide == "Comercial"
    assert condition[0].id_saida == "condicao-comercial:2026-09:00520815000100"
    assert condition[0].ref_origem == "sale"
    assert "isenção" in condition[0].motivo and "Nota bloqueada" in condition[0].motivo
    assert not any(item.id_saida == condition[0].id_saida for item in part.traces)
    assert [(row.tipo, row.quem_decide) for row in part.pending if row.tipo != "CONDICAO_COMERCIAL_NAO_DEFINIDA"] == [
        ("DIVERGENCIA_CHECKOUT", "Financeiro / Comercial"),
    ]
    _assert_traces(part)
    note_only = replace(casa, comments=None, note="cliente estratégico - ver c/ comercial")
    for flagged in (note_only, replace(casa, comments="Condição especial a definir")):
        flagged_part = close_checkout(_dataset((sale,), (flagged,)), period)
        assert flagged_part.commissions[0].comissao_brl == Decimal("2.00") and not flagged_part.invoices
        assert [row.tipo for row in flagged_part.pending] == ["CONDICAO_COMERCIAL_NAO_DEFINIDA"]
        _assert_traces(flagged_part)
    plain = close_checkout(_dataset((sale,), (replace(casa, comments=None, note="cliente estratégico"),)), period)
    assert plain.invoices[0].valor_brl == Decimal("2.00") and not plain.pending
    casa_august = replace(sale, occurred_on=date(2026, 8, 22))
    casa_pack_refund = replace(casa_refund, original_ref="sale", original_on=casa_august.occurred_on)
    part = close_checkout(_dataset((casa_august, casa_pack_refund), (casa,)), period)
    assert part.commissions[0].comissao_brl == Decimal("-2.00") and not part.divergences and not part.invoices
    assert {row.tipo: row.quem_decide for row in part.pending} == {
        "CONDICAO_COMERCIAL_NAO_DEFINIDA": "Comercial", "SALDO_NEGATIVO_CHECKOUT": "Financeiro",
    }
    _assert_traces(part)

    atlas = replace(sale, establishment_code="1076801732", merchant="Atlas", fee=Decimal("7.31"))
    historical = replace(
        contract, establishment_code=atlas.establishment_code, validity="01/01/2025",
        source=SourceRef("contratos.xlsx", "A6:L6", 6, "Taxas 2025"),
    )
    for contracts in ((), (historical,)):
        part = close_checkout(_dataset((atlas,), contracts), period)
        assert not part.commissions and not part.invoices and not part.divergences
        assert len(part.pending) == 1 and part.pending[0].quem_decide == "Comercial"
        assert part.pending[0].valor_brl == Decimal("7.31")
        _assert_traces(part)
    for contracts in ((contract, replace(contract, source=replace(contract.source, row=8))), (replace(contract, validity=None),)):
        part = close_checkout(_dataset((sale,), contracts), period)
        assert not part.commissions and not part.invoices and part.pending
        _assert_traces(part)

    band_entries = tuple(
        replace(sale, source=SourceRef("orbe.csv", f"band-{index}", index + 10), modality=modality, installments=installments, applied_rate=rate)
        for index, (modality, installments, rate) in enumerate((
            ("DEBITO", 1, Decimal("0.01")), ("CREDITO A VISTA", 1, Decimal("0.02")),
            ("CREDITO PARCELADO", 2, Decimal("0.03")), ("CREDITO PARCELADO", 6, Decimal("0.03")),
            ("CREDITO PARCELADO", 7, Decimal("0.04")), ("CREDITO PARCELADO", 12, Decimal("0.04")),
            ("PIX", 1, Decimal("0.005")),
        ))
    )
    part = close_checkout(_dataset(band_entries, (contract,)), period)
    assert part.commissions[0].comissao_brl == Decimal("17.50") and not part.divergences
    _assert_traces(part)
    # Sem vínculos EC→CNPJ vindos de conversas: contratos sem EC na planilha não casam com a Orbe.
    for index, (ec, taxpayer) in enumerate((
        ("1043300981", "66564512000199"), ("1043396662", "78576234000179"),
        ("1038867654", "07752116000126"), ("1076801732", "00000000000191"),
    )):
        alias_contract = replace(contract, cnpj=taxpayer, establishment_code=None,
            source=SourceRef("contratos.xlsx", f"A{20 + index}:L{20 + index}", 20 + index, "Taxas vigentes"))
        alias_sale = replace(sale, establishment_code=ec)
        part = close_checkout(_dataset((alias_sale,), (contract, alias_contract)), period)
        assert not part.commissions and not part.invoices and not part.divergences
        assert [(row.tipo, row.valor_brl, row.quem_decide) for row in part.pending] == [
            ("CHECKOUT_BLOQUEADO", Decimal("1.99"), "Comercial"),
        ]
        assert f"EC {ec}: encontrados 0 contratos" in part.pending[0].motivo
        assert "não comissão apurada" in part.pending[0].motivo
        _assert_traces(part)
    explicit = close_checkout(_dataset((replace(sale, establishment_code="1043300981"),),
        (replace(contract, cnpj="66564512000199", establishment_code="1043300981"),)), period)
    assert explicit.commissions[0].cnpj == "66564512000199" and explicit.commissions[0].comissao_brl == Decimal("2.00")
    _assert_traces(explicit)
    negative_sale = replace(sale, gross=Decimal("-1"))
    part = close_checkout(_dataset((negative_sale,), (contract,)), period)
    assert [(row.tipo, row.quem_decide) for row in part.pending] == [("CHECKOUT_BLOQUEADO", "Financeiro")]
    _assert_traces(part)
    for repeated in (
        (sale, replace(sale, source=replace(sale.source, row=3))),
        (advance, reversal, replace(reversal, source=SourceRef("orbe.csv", "reverse-again", 7))),
    ):
        try:
            close_checkout(_dataset(repeated, (contract,)), period)
        except DataPackError as error:
            assert error.source == repeated[-1].source
        else:
            raise AssertionError("Repetição de checkout foi contabilizada sem autorização.")


if __name__ == "__main__":
    check()
