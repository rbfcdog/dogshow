from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.application.remittances import close_remittances
from src.domain.closing import ClosingPart
from src.domain.models import Contract, Dataset, PartnerTerms, RemittanceEntry, SourceRef

TERMS = PartnerTerms(
    SourceRef("ponte.eml", "MIME:text/plain"), Decimal("0.5"), "75374179000146", "Ponte Câmbio S.A.",
)


def _assert_traces(result: ClosingPart) -> None:
    sums: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in result.traces:
        sums[row.id_saida] += row.valor
    output_values = {
        **{row.id_saida: row.comissao_brl for row in result.commissions},
        **{row.id_saida: row.diferenca_brl for row in result.divergences},
        **{row.id_saida: row.valor_brl for row in result.invoices},
        **{row.id_saida: row.valor_brl for row in result.pending if row.valor_brl is not None},
    }
    assert dict(sums) == output_values


def _kinds(result: ClosingPart) -> Counter:
    return Counter(row.tipo for row in result.pending)


def check() -> None:
    period = date(2026, 9, 1)

    def contract(index: int, name: str, rate: Decimal | None, **changes) -> Contract:
        return replace(Contract(
            SourceRef("contratos.xlsx", f"A{index}:L{index}", index, "Taxas vigentes"),
            name, f"{index:014d}", None, {}, rate, "01/07/2026", None,
        ), **changes)

    mundo = contract(15, "Mundo Aberto", Decimal("0.012"), note="spread negociado em jul/26")
    standard = contract(16, "Spread padrão", None)
    explicit = contract(17, "Spread explícito", Decimal("0.015"))
    negative_client = contract(18, "Cliente negativo", Decimal("0.015"))
    contracts = (mundo, standard, explicit, negative_client)
    base = RemittanceEntry(
        SourceRef("ponte.eml!operacoes.xlsx", "PC-MUNDO", 6, "Operações"),
        period, date(2026, 10, 1), mundo.cnpj, "Nome divergente do contrato", "USD",
        Decimal("10000"), Decimal("5.075"), Decimal("5"), Decimal("50750"),
        Decimal("262.01"), Decimal("0.015"), Decimal("999999"), Decimal("1"),
        "APROVADO C/ RESSALVA", None,
    )
    # Taxa Cliente 5.075 = PTAX 5 × 1,015 na precisão do relatório; sem percentual na planilha não há
    # como provar divergência, mesmo com Spread % "parecido" com o antigo padrão de 1,5%.
    padrao = replace(base, source=replace(base.source, ref="PC-PADRAO", row=7), cnpj=standard.cnpj,
        spread=Decimal("10.01"), spread_rate=Decimal("0.0151"))
    # Taxa Cliente bate com PTAX × (1 + spread) na precisão do relatório: sem divergência,
    # ainda que o Spread % reportado tenha ruído na quarta casa.
    rounded = replace(padrao, source=replace(base.source, ref="PC-PRECISAO", row=8), cnpj=explicit.cnpj,
        spread_rate=Decimal("0.015099"))
    # Taxa Cliente difere, mas a diferença econômica calculada é zero: nada a contestar.
    zero_difference = replace(rounded, source=replace(base.source, ref="PC-DIF-ZERO", row=9),
        customer_rate=Decimal("5.080"), spread_rate=Decimal("0.015"))
    negative = replace(base, source=replace(base.source, ref="PC-NEG", row=10), cnpj=negative_client.cnpj,
        customer_rate=Decimal("4.998"), spread=Decimal("-20.01"), spread_rate=Decimal("-0.0004"),
        note="Taxa travada no pedido (D-1)")
    duplicate = replace(base, source=replace(base.source, row=90), note="reprocessada")
    conflict = replace(base, source=replace(base.source, ref="PC-CONFLITO", row=91))
    changed_conflict = replace(conflict, source=replace(conflict.source, row=92), spread=Decimal("600"))
    old = replace(base, source=replace(base.source, ref="PC-AGOSTO", row=95), contracted_on=date(2026, 8, 31))
    remittances = (base, padrao, rounded, zero_difference, negative, duplicate, conflict, changed_conflict, old)
    dataset = Dataset((), contracts, remittances, (), (), {}, {}, TERMS)
    result = close_remittances(dataset, period)

    by_cnpj = {row.cnpj: row for row in result.commissions}
    assert {cnpj: row.comissao_brl for cnpj, row in by_cnpj.items()} == {
        mundo.cnpj: Decimal("131.01"), standard.cnpj: Decimal("5.01"),
        explicit.cnpj: Decimal("10.02"), negative_client.cnpj: Decimal("-10.01"),
    }
    assert by_cnpj[mundo.cnpj].lojista == "Mundo Aberto"
    assert all(row.linha_negocio == "REMESSA" for row in result.commissions)

    assert [(row.ref_origem, row.cnpj, row.taxa_contratada_pct, row.taxa_aplicada_pct, row.diferenca_brl)
        for row in result.divergences] == [
        ("PC-MUNDO", mundo.cnpj, Decimal("1.200"), Decimal("1.500"), Decimal("150.00")),
        ("PC-NEG", negative_client.cnpj, Decimal("1.500"), Decimal("-0.0400"), Decimal("-770.00")),
    ]
    validations = {row.ref_origem: row for row in result.pending if row.tipo == "VALIDAR_DIVERGENCIA_REMESSA"}
    assert {ref: row.valor_brl for ref, row in validations.items()} == {
        "PC-MUNDO": Decimal("150.00"), "PC-NEG": Decimal("-770.00"),
    }
    assert all(row.quem_decide == "Financeiro" for row in validations.values())
    assert "5.060" in validations["PC-MUNDO"].motivo and "nenhuma contestação presumida" in validations["PC-MUNDO"].motivo
    assert not {"PC-PADRAO", "PC-PRECISAO", "PC-DIF-ZERO", "PC-CONFLITO"} & {row.ref_origem for row in result.divergences}

    unresolved = [row for row in result.pending if row.tipo == "CONTRATO_REMESSA_NAO_RESOLVIDO"]
    assert [(row.ref_origem, row.valor_brl, row.quem_decide) for row in unresolved] == [
        ("PC-PADRAO", None, "Comercial / Financeiro"),
    ]
    assert "padrão" in unresolved[0].motivo

    negatives = [row for row in result.pending if row.tipo == "VALIDAR_SPREAD_NEGATIVO"]
    assert [(row.ref_origem, row.valor_brl, row.quem_decide) for row in negatives] == [
        ("PC-NEG", Decimal("-20.01"), "Financeiro"),
    ]
    negative_trace = [row for row in result.traces if row.id_saida == by_cnpj[negative_client.cnpj].id_saida]
    assert [(row.ref_origem, row.valor) for row in negative_trace] == [("PC-NEG", Decimal("-10.01"))]

    duplicated = [row for row in result.pending if row.tipo == "CORRIGIR_RELATORIO_PONTE"]
    assert len(duplicated) == 1 and duplicated[0].valor_brl == Decimal("262.01")
    assert duplicated[0].quem_decide == "Financeiro / Parceiro de câmbio"
    duplicate_traces = [row for row in result.traces if row.id_saida == duplicated[0].id_saida]
    assert len(duplicate_traces) == 1 and duplicate_traces[0].linha_origem == 90
    assert duplicate_traces[0].aba_origem == "Operações"
    blocked = [row for row in result.pending if row.tipo == "CONFLITO_OPERACAO_REMESSA"]
    assert [(row.ref_origem, row.valor_brl) for row in blocked] == [("PC-CONFLITO", None)]
    assert _kinds(result) == {
        "VALIDAR_DIVERGENCIA_REMESSA": 2, "CONTRATO_REMESSA_NAO_RESOLVIDO": 1,
        "VALIDAR_SPREAD_NEGATIVO": 1, "CORRIGIR_RELATORIO_PONTE": 1, "CONFLITO_OPERACAO_REMESSA": 1,
        "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA": 1,
    }
    assert not {"CONTESTAR_REMESSA_PONTE", "CONFERIR_REPASSE_FUTURO_PONTE"} & set(_kinds(result))

    assert len(result.invoices) == 1
    invoice = result.invoices[0]
    assert (invoice.tomador_cnpj, invoice.tomador, invoice.linha_negocio) == (TERMS.cnpj, TERMS.name, "REMESSA")
    assert invoice.valor_brl == Decimal("136.03")
    assert invoice.valor_brl == sum((row.comissao_brl for row in result.commissions), Decimal("0"))
    invoice_rows = [row for row in result.traces if row.id_saida == invoice.id_saida]
    assert {row.ref_origem for row in invoice_rows} == {"PC-MUNDO", "PC-PADRAO", "PC-PRECISAO", "PC-DIF-ZERO", "PC-NEG"}
    assert all(row.linha_origem != 90 for row in invoice_rows)
    commission_rule = next(row.regra for row in result.traces if row.id_saida == by_cnpj[mundo.cnpj].id_saida)
    assert "participação 0.5" in commission_rule and "ponte.eml/MIME:text/plain" in commission_rule
    _assert_traces(result)

    assert close_remittances(replace(dataset, remittances=tuple(reversed(remittances))), period) == result
    changed_estimate = close_remittances(replace(dataset, remittances=tuple(
        replace(row, estimated_spread=Decimal("0")) for row in remittances)), period)
    assert changed_estimate == result
    held = close_remittances(replace(dataset, remittances=(replace(base, status="EM ANALISE"),)), period)
    assert held.invoices[0].valor_brl == Decimal("131.01")
    assert close_remittances(dataset, date(2026, 11, 1)) == ClosingPart()
    august = close_remittances(dataset, date(2026, 8, 1))
    assert [row.comissao_brl for row in august.commissions] == [Decimal("131.01")]
    assert august.invoices[0].valor_brl == Decimal("131.01")
    cross_month_conflict = replace(conflict, contracted_on=date(2026, 8, 31))
    cross_month = close_remittances(replace(dataset, remittances=(conflict, cross_month_conflict, base)), period)
    assert [row.comissao_brl for row in cross_month.commissions] == [Decimal("131.01")]
    assert _kinds(cross_month)["CONFLITO_OPERACAO_REMESSA"] == 1

    # Sem participação documentada no corpo do e-mail não há comissão nem nota.
    for terms in (None, replace(TERMS, participation=None), replace(TERMS, participation=Decimal("1.5"))):
        undocumented = close_remittances(replace(dataset, partner_terms=terms), period)
        assert undocumented.commissions == () and undocumented.invoices == ()
        missing_share = [row for row in undocumented.pending if row.tipo == "PARTICIPACAO_NAO_DOCUMENTADA"]
        assert {row.ref_origem for row in missing_share} == {"PC-MUNDO", "PC-PADRAO", "PC-PRECISAO", "PC-DIF-ZERO", "PC-NEG"}
        assert all(row.valor_brl is None and row.quem_decide == "Financeiro" for row in missing_share)
        assert "VALIDAR_NOTA_REMESSA" not in _kinds(undocumented)
        assert [row.ref_origem for row in undocumented.divergences] == ["PC-MUNDO", "PC-NEG"]
        _assert_traces(undocumented)
    custom_share = close_remittances(replace(dataset, partner_terms=replace(TERMS, participation=Decimal("0.4")),
        remittances=(base,)), period)
    assert custom_share.commissions[0].comissao_brl == Decimal("104.80")

    # Nota de remessa só com tomador extraído do e-mail e saldo positivo.
    for terms in (replace(TERMS, cnpj=None), replace(TERMS, name=None)):
        no_taker = close_remittances(replace(dataset, partner_terms=terms), period)
        assert no_taker.invoices == () and len(no_taker.commissions) == 4
        assert [(row.tipo, row.valor_brl) for row in no_taker.pending if row.tipo == "VALIDAR_NOTA_REMESSA"] == [
            ("VALIDAR_NOTA_REMESSA", None),
        ]
        _assert_traces(no_taker)
    only_negative = close_remittances(replace(dataset, remittances=(negative,)), period)
    assert [row.comissao_brl for row in only_negative.commissions] == [Decimal("-10.01")]
    assert only_negative.invoices == ()
    assert _kinds(only_negative) == {
        "VALIDAR_DIVERGENCIA_REMESSA": 1, "VALIDAR_SPREAD_NEGATIVO": 1, "VALIDAR_NOTA_REMESSA": 1,
        "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA": 1,
    }
    _assert_traces(only_negative)

    # Contrato ausente, ambíguo ou fora de vigência não recebe taxa padrão nem divergência.
    for candidates in ((), (mundo, replace(mundo, remittance_rate=Decimal("0.02"),
            source=replace(mundo.source, ref="A40:L40", row=40)))):
        unresolved_result = close_remittances(replace(dataset, contracts=candidates, remittances=(base,)), period)
        assert unresolved_result.commissions[0].comissao_brl == Decimal("131.01")
        assert unresolved_result.commissions[0].lojista == base.merchant
        assert unresolved_result.invoices[0].valor_brl == Decimal("131.01") and unresolved_result.divergences == ()
        assert _kinds(unresolved_result) == {"CONTRATO_REMESSA_NAO_RESOLVIDO": 1, "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA": 1}
    inactive = replace(mundo, note="INATIVO")
    undated = replace(mundo, validity=None)
    before = replace(mundo, validity="até 30/06/2026")
    future = replace(mundo, validity="a partir de 01/10/2026")
    current = close_remittances(replace(dataset, contracts=(inactive, undated, before, future, mundo),
        remittances=(base,)), period)
    assert current.commissions[0].comissao_brl == Decimal("131.01")
    assert [row.taxa_contratada_pct for row in current.divergences] == [Decimal("1.200")]
    no_current = close_remittances(replace(dataset, contracts=(inactive, undated, before, future),
        remittances=(base,)), period)
    assert no_current.divergences == () and _kinds(no_current) == {"CONTRATO_REMESSA_NAO_RESOLVIDO": 1, "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA": 1}


if __name__ == "__main__":
    check()
