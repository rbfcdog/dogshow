from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.application.remittance_reconciliation import reconcile_remittance_resources
from src.application.remittances import close_remittances
from src.domain.closing import ClosingPart
from src.domain.models import BankEntry, Contract, Dataset, RemittanceEntry, SourceRef


RESOURCE_TYPES = (
    "RECURSOS_REMESSA_DIVERGENTES",
    "LIQUIDACAO_REMESSA_DIVERGENTE",
    "CLIENTE_RECURSO_REMESSA_NAO_RESOLVIDO",
    "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA",
)


def _resources(result: ClosingPart) -> ClosingPart:
    pending = tuple(row for row in result.pending if row.tipo in RESOURCE_TYPES)
    identifiers = {row.id_saida for row in pending}
    return ClosingPart(
        pending=pending,
        traces=tuple(row for row in result.traces if row.id_saida in identifiers),
    )


def _assert_pending(
    result: ClosingPart,
    kind: str,
    ref: str | None,
    amount: Decimal | None,
    contributions: tuple[tuple[SourceRef, Decimal], ...] = (),
) -> None:
    rows = [row for row in result.pending if row.tipo == kind and (ref is None or row.ref_origem == ref)]
    assert len(rows) == 1, (kind, ref, rows)
    pending = rows[0]
    assert pending.valor_brl == amount
    assert pending.quem_decide == "Helena / Ponte Câmbio"
    traces = [row for row in result.traces if row.id_saida == pending.id_saida]
    assert len(traces) == len(contributions)
    assert {
        (row.arquivo_origem, row.ref_origem, row.linha_origem, row.aba_origem, row.valor)
        for row in traces
    } == {
        (source.file, source.ref, source.row, source.sheet, value)
        for source, value in contributions
    }
    if amount is not None:
        assert sum((row.valor for row in traces), Decimal("0")) == amount
        assert pending.ref_origem in {source.ref for source, _ in contributions}


def check() -> None:
    period = date(2026, 9, 1)
    contract = Contract(
        SourceRef("contratos.xlsx", "C-PONTE", 2, "Vigentes"),
        "Ponte Aérea Estudantil Ltda", "95739378000118", None, {},
        Decimal("0.015"), "01/07/2026", None,
    )
    contracts = (
        contract,
        replace(contract, source=replace(contract.source, ref="C-NORTE", row=3),
                cnpj="66564512000199", merchant="Agência Norte Viagens"),
        replace(contract, source=replace(contract.source, ref="C-SUL", row=4),
                cnpj="78576234000179", merchant="Agência Sul Viagens"),
    )
    base = RemittanceEntry(
        SourceRef("ponte.eml!operacoes.xlsx", "PC-PONTE", 6, "Operações"),
        period, date(2026, 10, 2), contract.cnpj, "Agência Exterior", "USD",
        Decimal("80000"), Decimal("5"), Decimal("5"), Decimal("400000"),
        Decimal("100"), Decimal("0.015"), Decimal("999999"), Decimal("2000"),
        "APROVADO", None,
    )
    negative = replace(
        base, source=replace(base.source, ref="PC-NEGATIVA", row=7),
        contracted_on=date(2026, 9, 2), brl_amount=Decimal("50000"),
        iof=Decimal("249.03"), spread=Decimal("-20"), spread_rate=Decimal("-0.0004"),
    )
    north = replace(
        base, source=replace(base.source, ref="PC-NORTE", row=8),
        cnpj=contracts[1].cnpj, merchant="Intercâmbio Horizonte Norte",
        brl_amount=Decimal("100"), iof=Decimal("2.03"),
    )
    south = replace(
        base, source=replace(base.source, ref="PC-SUL", row=9),
        cnpj=contracts[2].cnpj, merchant="Intercâmbio Horizonte Sul Ltda",
        brl_amount=Decimal("200"), iof=Decimal("5.07"),
    )
    zero = replace(
        base, source=replace(base.source, ref="PC-ZERO", row=10),
        cnpj="07752116000126", merchant="Órbita Educação Exterior",
        brl_amount=Decimal("300"), iof=Decimal("6.11"), spread=Decimal("0"),
    )
    operations = (base, negative, north, south, zero)
    duplicate = replace(base, source=replace(base.source, row=90), note="reprocessada")
    pix = BankEntry(
        SourceRef("meridiano.ofx", "PIX-PONTE-1", 11), "MERIDIANO", period,
        "PIX RECEB PONTE AEREA ESTUDANTIL", Decimal("470000"),
    )
    pix_tail = replace(pix, source=replace(pix.source, ref="PIX-PONTE-2", row=12), amount=Decimal("489.03"))
    north_pix = replace(
        pix, source=replace(pix.source, ref="PIX-NORTE", row=13),
        description="PIX RECEB INTERCAMBIO HORIZONTE NORTE SERVICOS LTDA", amount=Decimal("102.03"),
    )
    south_pix = replace(
        pix, source=replace(pix.source, ref="PIX-SUL", row=14),
        description="PIX RECEB INTERCAMBIO HORIZONTE SUL", amount=Decimal("205.07"),
    )
    zero_pix = replace(
        pix, source=replace(pix.source, ref="PIX-ZERO", row=15),
        description="PIX RECEB ORBITA EDUCACAO EXTERIOR", amount=Decimal("306.11"),
    )
    ted = replace(
        pix, source=replace(pix.source, ref="TED-1", row=16),
        description="TED ENVIADA PONTE CAMBIO SA LIQ OPERACOES", amount=Decimal("-402613.21"),
    )
    negative_ted = replace(
        ted, source=replace(ted.source, ref="TED-2", row=17),
        occurred_on=negative.contracted_on, amount=Decimal("-50249.03"),
    )
    ignored = (
        replace(pix, source=replace(pix.source, ref="PIX-AURORA"), bank="AURORA", amount=Decimal("88888")),
        replace(pix, source=replace(pix.source, ref="PIX-AGOSTO"), occurred_on=date(2026, 8, 31), amount=Decimal("77777")),
        replace(ted, source=replace(ted.source, ref="TED-RECEBIDA"),
                description="TED RECEB PONTE CAMBIO SA REPASSE COMISSAO", amount=Decimal("66666")),
    )
    dataset = Dataset(
        (), contracts, (*operations, duplicate),
        (pix, pix_tail, north_pix, south_pix, zero_pix, ted, negative_ted, *ignored),
        (), {}, {},
    )
    result = reconcile_remittance_resources(dataset, period, operations)
    assert len(result.pending) == 1
    _assert_pending(result, "RECURSOS_REMESSA_DIVERGENTES", base.source.ref, Decimal("18240.00"), (
        (pix.source, Decimal("470000.00")), (pix_tail.source, Decimal("489.03")),
        (base.source, Decimal("-402000.00")), (negative.source, Decimal("-50249.03")),
    ))
    closed = close_remittances(dataset, period)
    assert _resources(closed) == result
    assert not any(row.tipo == "CONFLITO_OPERACAO_REMESSA" for row in closed.pending)
    assert len([row for row in closed.pending if row.tipo == "CORRIGIR_RELATORIO_PONTE" and row.ref_origem == base.source.ref]) == 1
    assert all(row.linha_origem != duplicate.source.row for row in result.traces)
    reordered = replace(
        dataset, contracts=tuple(reversed(contracts)),
        remittances=tuple(reversed(dataset.remittances)), banks=tuple(reversed(dataset.banks)),
    )
    assert reconcile_remittance_resources(reordered, period, tuple(reversed(operations))) == result
    assert close_remittances(reordered, period) == closed

    for difference in (Decimal("7.00"), Decimal("-7.00")):
        changed_ted = replace(negative_ted, amount=negative_ted.amount - difference)
        changed_north = replace(north_pix, amount=Decimal("100.00"))
        changed = replace(dataset, banks=(pix, pix_tail, changed_north, south_pix, zero_pix, ted, changed_ted))
        changed_result = reconcile_remittance_resources(changed, period, operations)
        assert len(changed_result.pending) == 3
        _assert_pending(changed_result, "RECURSOS_REMESSA_DIVERGENTES", base.source.ref, Decimal("18240.00"), (
            (pix.source, pix.amount), (pix_tail.source, pix_tail.amount),
            (base.source, Decimal("-402000.00")), (negative.source, Decimal("-50249.03")),
        ))
        _assert_pending(changed_result, "RECURSOS_REMESSA_DIVERGENTES", north.source.ref, Decimal("-2.03"), (
            (changed_north.source, changed_north.amount), (north.source, Decimal("-102.03")),
        ))
        _assert_pending(changed_result, "LIQUIDACAO_REMESSA_DIVERGENTE", None, difference, (
            (changed_ted.source, -changed_ted.amount), (negative.source, Decimal("-50249.03")),
        ))

    unresolved = tuple(
        replace(pix, source=replace(pix.source, ref=f"PIX-SEM-CLIENTE-{index}", row=30 + index),
                description=description, amount=Decimal(amount))
        for index, (description, amount) in enumerate((
            ("PIX RECEB 38.686.002/0001-80 PONTE AEREA ESTUDANTIL", "12.34"),
            ("PIX RECEB 95.739.378/0001-18 66.564.512/0001-99", "23.45"),
            ("PIX RECEB INTERCAMBIO HORIZONTE", "34.56"),
            ("PIX RECEB PONTE", "45.67"),
            ("PIX RECEB PONTE CAMBIO SA", "56.78"),
        ))
    )
    changed_ted = replace(negative_ted, amount=negative_ted.amount - Decimal("7"))
    ambiguous = replace(dataset, banks=(pix, pix_tail, north_pix, south_pix, zero_pix, ted, changed_ted, *unresolved))
    ambiguous_result = reconcile_remittance_resources(ambiguous, period, operations)
    assert len(ambiguous_result.pending) == len(unresolved) + 2
    assert not any(row.tipo == "RECURSOS_REMESSA_DIVERGENTES" for row in ambiguous_result.pending)
    for entry in unresolved:
        _assert_pending(ambiguous_result, "CLIENTE_RECURSO_REMESSA_NAO_RESOLVIDO", entry.source.ref,
                        entry.amount, ((entry.source, entry.amount),))
    _assert_pending(ambiguous_result, "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA", None, None)
    _assert_pending(ambiguous_result, "LIQUIDACAO_REMESSA_DIVERGENTE", None, Decimal("7.00"), (
        (changed_ted.source, -changed_ted.amount), (negative.source, Decimal("-50249.03")),
    ))
    assert reconcile_remittance_resources(
        replace(ambiguous, banks=tuple(reversed(ambiguous.banks)),
                remittances=tuple(reversed(ambiguous.remittances)), contracts=tuple(reversed(contracts))),
        period, tuple(reversed(operations)),
    ) == ambiguous_result

    for banks in ((), ignored[:1], ignored[1:2]):
        unavailable = reconcile_remittance_resources(replace(dataset, banks=banks), period, operations)
        assert len(unavailable.pending) == 1 and unavailable.traces == ()
        _assert_pending(unavailable, "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA", None, None)
    conflict = replace(base, source=replace(base.source, row=91), brl_amount=Decimal("400001"))
    conflicting = replace(dataset, remittances=(*dataset.remittances, conflict))
    incomplete = reconcile_remittance_resources(conflicting, period, operations[1:])
    assert len(incomplete.pending) == 1 and incomplete.traces == ()
    _assert_pending(incomplete, "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA", None, None)
    assert _resources(close_remittances(conflicting, period)) == incomplete
    assert reconcile_remittance_resources(replace(dataset, banks=(), remittances=()), period, ()) == ClosingPart()


if __name__ == "__main__":
    check()
