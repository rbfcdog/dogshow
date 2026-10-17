from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.application.remittances import close_remittances
from src.domain.models import Contract, Dataset, RemittanceEntry, SourceRef


def check() -> None:
    period = date(2026, 9, 1)
    contracts = tuple(
        Contract(
            SourceRef("contratos.xlsx", f"C{index}", index + 2, "Vigentes"),
            "Mundo Aberto" if index == 0 else f"Lojista {index}",
            f"{index + 1:014d}", None, {},
            Decimal("0.012") if index == 0 else None,
            "01/07/2026", None,
        )
        for index in range(14)
    )
    base = RemittanceEntry(
        SourceRef("ponte.eml!operacoes.xlsx", "PC-MUNDO", 6, "Operações"),
        period, date(2026, 10, 1), contracts[0].cnpj, "Nome divergente do contrato", "USD",
        Decimal("10000"), Decimal("5.075"), Decimal("5"), Decimal("50750"),
        Decimal("262.01"), Decimal("0.015"), Decimal("999999"), Decimal("1"),
        "APROVADO C/ RESSALVA", None,
    )
    regular = tuple(
        replace(
            base,
            source=replace(base.source, ref=f"PC-REG-{index}", row=10 + index),
            cnpj=contract.cnpj, spread=Decimal("10.01"), status="APROVADO",
        )
        for index, contract in enumerate(contracts[1:], 1)
    )
    negatives = tuple(
        replace(
            base, source=replace(base.source, ref=f"PC-NEG-{index}", row=30 + index),
            cnpj=contracts[1].cnpj, spread=Decimal("-20.01"), spread_rate=Decimal("-0.0004"),
            note="Taxa travada no pedido (D-1)",
        )
        for index in range(4)
    )
    duplicate = replace(base, source=replace(base.source, row=90), note="reprocessada")
    conflict = replace(base, source=replace(base.source, ref="PC-CONFLITO", row=91))
    changed_conflict = replace(conflict, source=replace(conflict.source, row=92), spread=Decimal("600"))
    exact_limit = replace(
        base, source=replace(base.source, ref="PC-LIMITE", row=93),
        cnpj=contracts[1].cnpj, spread=Decimal("10.01"), spread_rate=Decimal("0.0151"),
    )
    below_limit = replace(
        exact_limit, source=replace(base.source, ref="PC-ABAIXO", row=94),
        spread_rate=Decimal("0.015099"),
    )
    old = replace(base, source=replace(base.source, ref="PC-AGOSTO", row=95), contracted_on=date(2026, 8, 31), settled_on=period)
    empty = replace(base, source=replace(base.source, ref="PC-ZERO", row=96), spread=Decimal("0"), spread_rate=Decimal("0.012"))
    dataset = Dataset(
        (), contracts,
        (base, *regular, *negatives, duplicate, conflict, changed_conflict, exact_limit, below_limit, old, empty),
        (), (), {}, {},
    )
    result = close_remittances(dataset, period)
    by_cnpj = {row.cnpj: row for row in result.commissions}
    assert len(by_cnpj) == 14
    assert by_cnpj[contracts[0].cnpj].lojista == "Mundo Aberto"
    assert by_cnpj[contracts[0].cnpj].comissao_brl == Decimal("131.01")
    assert by_cnpj[contracts[1].cnpj].comissao_brl == Decimal("15.03")
    assert all(by_cnpj[contract.cnpj].comissao_brl == Decimal("5.01") for contract in contracts[2:])
    assert len(result.invoices) == 1
    invoice = result.invoices[0]
    assert invoice.tomador_cnpj == "75374179000146" and invoice.linha_negocio == "REMESSA"
    assert invoice.valor_brl == Decimal("206.16")
    assert invoice.valor_brl == sum((row.comissao_brl for row in result.commissions), Decimal("0"))
    by_ref = {row.ref_origem: row for row in result.divergences}
    assert len(by_ref) == 6
    assert by_ref["PC-MUNDO"].taxa_contratada_pct == Decimal("1.200")
    assert by_ref["PC-MUNDO"].diferenca_brl == Decimal("150.00")
    assert by_ref["PC-LIMITE"].taxa_contratada_pct == Decimal("1.500")
    assert by_ref["PC-LIMITE"].diferenca_brl == Decimal("5.00")
    assert all(by_ref[f"PC-NEG-{index}"].diferenca_brl == Decimal("-770.00") for index in range(4))
    assert "PC-ABAIXO" not in by_ref and "PC-CONFLITO" not in by_ref
    duplicated = [row for row in result.pending if row.tipo == "CORRIGIR_RELATORIO_PONTE"]
    assert len(duplicated) == 1 and duplicated[0].valor_brl == Decimal("262.01")
    duplicate_traces = [row for row in result.traces if row.id_saida == duplicated[0].id_saida]
    assert len(duplicate_traces) == 1 and duplicate_traces[0].linha_origem == 90
    assert duplicate_traces[0].aba_origem == "Operações"
    blocked = [row for row in result.pending if row.tipo == "CONFLITO_OPERACAO_REMESSA"]
    assert len(blocked) == 1 and blocked[0].valor_brl is None
    contested = {row.ref_origem: row for row in result.pending if row.tipo == "CONTESTAR_REMESSA_PONTE"}
    assert len(contested) == 6
    assert contested["PC-MUNDO"].valor_brl == Decimal("150.00")
    assert all(contested[f"PC-NEG-{index}"].valor_brl == Decimal("-770.00") for index in range(4))
    assert all(row.quem_decide == "Helena" for row in contested.values())
    receipt = [row for row in result.pending if row.tipo == "CONFERIR_REPASSE_FUTURO_PONTE"]
    assert len(receipt) == 1 and receipt[0].valor_brl == invoice.valor_brl
    expected_refs = {"PC-MUNDO", "PC-LIMITE", "PC-ABAIXO", *(row.source.ref for row in regular)}
    for output in (invoice, receipt[0]):
        rows = [row for row in result.traces if row.id_saida == output.id_saida]
        assert {row.ref_origem for row in rows} == expected_refs
        assert len(rows) == len(expected_refs)
        assert all(row.linha_origem != 90 for row in rows)
    sums = defaultdict(lambda: Decimal("0"))
    for row in result.traces:
        sums[row.id_saida] += row.valor
    output_values = {
        **{row.id_saida: row.comissao_brl for row in result.commissions},
        **{row.id_saida: row.diferenca_brl for row in result.divergences},
        **{row.id_saida: row.valor_brl for row in result.invoices},
        **{row.id_saida: row.valor_brl for row in result.pending if row.valor_brl is not None},
    }
    assert dict(sums) == output_values
    reversed_result = close_remittances(replace(dataset, remittances=tuple(reversed(dataset.remittances))), period)
    assert reversed_result == result
    assert close_remittances(dataset, date(2026, 11, 1)).invoices == ()
    august = close_remittances(dataset, date(2026, 8, 1))
    assert august.invoices[0].valor_brl == Decimal("131.01")
    missing = close_remittances(replace(dataset, contracts=(), remittances=(base,)), period)
    assert missing.commissions[0].comissao_brl == Decimal("131.01")
    assert missing.invoices[0].valor_brl == Decimal("131.01") and missing.divergences == ()
    assert any(row.tipo == "CONTRATO_REMESSA_NAO_RESOLVIDO" and row.valor_brl is None for row in missing.pending)
    ambiguous = close_remittances(replace(dataset, contracts=(contracts[0], replace(contracts[0], remittance_rate=Decimal("0.02"))), remittances=(base,)), period)
    assert ambiguous.commissions[0].comissao_brl == Decimal("131.01")
    assert ambiguous.invoices[0].valor_brl == Decimal("131.01") and ambiguous.divergences == ()
    assert any(row.tipo == "CONTRATO_REMESSA_NAO_RESOLVIDO" and row.valor_brl is None for row in ambiguous.pending)
    inactive = replace(contracts[0], note="INATIVO")
    undated = replace(contracts[0], validity=None)
    before = replace(contracts[0], validity="até 30/06/2026")
    future = replace(contracts[0], validity="a partir de 01/10/2026")
    current = close_remittances(replace(dataset, contracts=(inactive, undated, before, future, contracts[0]), remittances=(base,)), period)
    assert current.commissions[0].comissao_brl == Decimal("131.01")
    assert current.divergences[0].taxa_contratada_pct == Decimal("1.200")
    cross_month_conflict = replace(conflict, contracted_on=date(2026, 8, 31))
    cross_month = close_remittances(replace(dataset, remittances=(conflict, cross_month_conflict, base)), period)
    assert len(cross_month.commissions) == 1 and cross_month.commissions[0].comissao_brl == Decimal("131.01")
    assert any(row.tipo == "CONFLITO_OPERACAO_REMESSA" and row.valor_brl is None for row in cross_month.pending)
    changed_estimate = close_remittances(replace(dataset, remittances=tuple(replace(row, estimated_spread=Decimal("0")) for row in dataset.remittances)), period)
    assert changed_estimate == result
    unknown_negative = close_remittances(replace(dataset, contracts=(), remittances=(negatives[0],)), period)
    assert unknown_negative.invoices == () and unknown_negative.commissions == ()
    assert any(row.tipo == "CONTESTAR_REMESSA_PONTE" and row.valor_brl == Decimal("-20.01") for row in unknown_negative.pending)
    held = close_remittances(replace(dataset, remittances=(replace(base, status="EM ANALISE"),)), period)
    assert held.invoices[0].valor_brl == Decimal("131.01")
    assert held.commissions[0].comissao_brl == Decimal("131.01")


if __name__ == "__main__":
    check()
