from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.application.banking import classify_banks
from src.domain.closing import ClosingPart
from src.domain.models import Account, BankEntry, Dataset, SourceRef


def _assert_traces(result: ClosingPart, entries: tuple[BankEntry, ...]) -> None:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    sources = {(entry.source.file, entry.source.ref): entry.source for entry in entries}
    identifiers = {movement.id_saida for movement in result.movements}
    identifiers.update(pending.id_saida for pending in result.pending)
    assert len(identifiers) == len(result.movements) + len(result.pending)
    for contribution in result.traces:
        assert contribution.id_saida in identifiers
        source = sources[(contribution.arquivo_origem, contribution.ref_origem)]
        assert contribution.linha_origem == source.row
        assert contribution.aba_origem == source.sheet
        totals[contribution.id_saida] += contribution.valor
    for movement in result.movements:
        assert movement.id_saida in totals
        assert totals[movement.id_saida] == movement.valor
    for pending in result.pending:
        assert pending.id_saida in totals
        assert totals[pending.id_saida] == pending.valor_brl


def check() -> None:
    period = date(2026, 9, 1)
    scenarios = (
        ("L9", "AURORA", 1, "TED RECEBIDA 99.876.669/0001-09 ORBE ADQUIRENCIA SA", "66026", "1.1.03.001"),
        ("L15", "AURORA", 4, "PAGTO SALARIOS - FOLHA 08/2026", "-184732.18", "2.1.04.001"),
        ("L54", "AURORA", 18, "GPS INSS 08/2026", "-51320.66", "2.1.04.002"),
        ("L55", "AURORA", 18, "GRF FGTS 08/2026", "-14778.57", "2.1.04.003"),
        ("L42", "AURORA", 10, "PAGTO ISS SAO PAULO DAMSP 08/2026", "-21877.40", "2.1.03.003"),
        ("L57", "AURORA", 21, "DEBITO DARF 0561 IRRF", "-22104.35", "2.1.03.004"),
        ("L86", "AURORA", 25, "DEBITO DARF 8109 PIS", "-9811.20", "2.1.03.001"),
        ("L87", "AURORA", 25, "DEBITO DARF 2172 COFINS", "-45282.46", "2.1.03.002"),
        ("MRD2026090132", "MERIDIANO", 4, "TED RECEB PONTE CAMBIO SA REPASSE COMISSAO 08/2026", "61884.12", "1.1.03.002"),
        ("L10", "AURORA", 1, "TAR PACOTE SERVICOS PJ", "-389", "4.2.01"),
        ("L16", "AURORA", 5, "BOLETO CONDOMINIO ED. FARIA LIMA 3311", "-28450", "4.2.02"),
        ("L47", "AURORA", 15, "DEB AUT AMAZON WEB SERVICES BR", "-41208.77", "4.2.03"),
        ("L48", "AURORA", 15, "CARTAO PJ - GOOGLE WORKSPACE", "-2210", "4.2.04"),
        ("MRD2026090292", "MERIDIANO", 14, "SEGURO EMPRESARIAL PARC 09/12", "-1830.44", "4.2.05"),
        ("L53", "AURORA", 18, "PIX ENVIADO CONTABILIDADE EXATA LTDA", "-6800", "4.2.06"),
        ("MRD2026090586", "MERIDIANO", 24, "IOF S/ OPERACAO CREDITO", "-48.21", "4.3.01"),
        ("L50", "AURORA", 16, "PIX RECEBIDO 38686002000180 SEM IDENTIFICACAO", "7350", "1.1.09.002"),
        ("L65", "AURORA", 23, "DEVOLUCAO PIX - MUNDO ABERTO INTERC", "12400", ""),
        ("L18", "AURORA", 8, "PIX ENVIADO ROTA GLOBAL INTERCAMBIO LT", "-77106.46", ""),
        ("MRD2026090425", "MERIDIANO", 17, "PIX RECEB PONTE AEREA ESTUDANTIL L", "18240", ""),
        ("MRD2026090094", "MERIDIANO", 1, "TED ENVIADA PONTE CAMBIO SA LIQ OPERACOES", "-240619.49", "2.1.02.001"),
        ("MRD2026090143", "MERIDIANO", 4, "APLICACAO AUTOMATICA CDB DI", "-161736", "1.1.02.001"),
        ("MRD2026090254", "MERIDIANO", 10, "RESGATE AUTOMATICO CDB DI", "162128", "1.1.02.001"),
        ("MRD2026090680", "MERIDIANO", 30, "RENDIMENTO APLIC AUTOMATICA", "3912.66", "3.2.01"),
        ("L12", "AURORA", 2, "TRANSF ENTRE CONTAS P/ BCO MERIDIANO", "-350000", "1.1.09.001"),
        ("MRD2026090107", "MERIDIANO", 2, "TED RECEBIDA TARVO PAGAMENTOS LTDA - MESMA TITULARIDADE", "350000", "1.1.09.001"),
        ("L44", "AURORA", 11, "TRANSF ENTRE CONTAS P/ BCO MERIDIANO", "-420000", "1.1.09.001"),
        ("MRD2026090274", "MERIDIANO", 11, "TED RECEBIDA TARVO PAGAMENTOS LTDA - MESMA TITULARIDADE", "420000", "1.1.09.001"),
        ("L58", "AURORA", 21, "TRANSF ENTRE CONTAS P/ BCO MERIDIANO", "-380000", "1.1.09.001"),
        ("MRD2026090454", "MERIDIANO", 21, "TED RECEBIDA TARVO PAGAMENTOS LTDA - MESMA TITULARIDADE", "380000", "1.1.09.001"),
        ("L92", "AURORA", 30, "TRANSF ENTRE CONTAS P/ BCO MERIDIANO", "-300000", "1.1.09.001"),
        ("MRD-UNKNOWN", "MERIDIANO", 30, "CREDITO SEM DOCUMENTO", "92.71", ""),
    )
    entries = tuple(
        BankEntry(
            SourceRef("aurora.csv" if bank == "AURORA" else "meridiano.ofx", ref, int(ref[1:]) if bank == "AURORA" else None),
            bank, date(2026, 9, day), description, Decimal(amount),
            Decimal("-1920088.60") if ref == "L92" else None,
        )
        for ref, bank, day, description, amount, _ in scenarios
    )
    # Contas que existiam como contrapartida no modelo anterior continuam no plano: a ausência de
    # classificação deve vir da falta de evidência, não da falta de conta.
    accounts = tuple(
        Account(SourceRef("contas.csv", code), code, code, "ANALITICA", "D", True)
        for code in sorted({scenario[-1] for scenario in scenarios} - {""} | {"2.1.01.001", "4.3.02"})
    )
    dataset = Dataset((), (), (), entries, accounts, {}, {})
    result = classify_banks(dataset, period)
    assert len(result.movements) == len(entries)
    movements = {movement.ref_origem: movement for movement in result.movements}
    for ref, bank, day, description, amount, expected_account in scenarios:
        movement = movements[ref]
        assert movement.conta_codigo == expected_account, (ref, movement.conta_codigo)
        assert movement.valor == Decimal(amount)
    exact = {"MRD2026090143", "MRD2026090254", "MRD2026090680", "L12", "MRD2026090107", "L44",
        "MRD2026090274", "L58", "MRD2026090454", "L92"}
    unclassified = {"L65", "L18", "MRD2026090425", "MRD-UNKNOWN"}
    for ref, movement in movements.items():
        expected = Decimal("1") if ref in exact else Decimal("0") if ref in unclassified else Decimal("0.80")
        assert movement.confianca == expected, (ref, movement.confianca)
        assert movement.metodo == "REGRA"
    by_kind: dict[str, set[str]] = defaultdict(set)
    for item in result.pending:
        by_kind[item.tipo].add(item.ref_origem)
    assumed = set(movements) - exact - unclassified
    assert dict(by_kind) == {
        "VALIDAR_RECONHECIMENTO_CONTABIL": assumed,
        "IDENTIFICAR_PIX": {"L50"},
        "CLASSIFICACAO_BANCARIA": unclassified,
        "CONCILIAR_TRANSFERENCIA_PROPRIA": {"L92"},
    }
    assert len(result.pending) == len(assumed) + 1 + len(unclassified) + 1
    assert all(item.quem_decide == "Financeiro" for item in result.pending)
    pending = {(item.tipo, item.ref_origem): item for item in result.pending}
    assert pending[("IDENTIFICAR_PIX", "L50")].valor_brl == Decimal("7350")
    assert pending[("CLASSIFICACAO_BANCARIA", "L65")].valor_brl == Decimal("12400")
    assert "não comprovad" in pending[("CLASSIFICACAO_BANCARIA", "L65")].motivo
    assert pending[("CLASSIFICACAO_BANCARIA", "L18")].valor_brl == Decimal("-77106.46")
    assert "PIX enviado" in pending[("CLASSIFICACAO_BANCARIA", "L18")].motivo
    assert "finalidade" in pending[("CLASSIFICACAO_BANCARIA", "MRD2026090425")].motivo
    assert pending[("CONCILIAR_TRANSFERENCIA_PROPRIA", "L92")].valor_brl == Decimal("-300000")
    assert "provisão" in pending[("VALIDAR_RECONHECIMENTO_CONTABIL", "L15")].motivo
    assert all(movement.conta_codigo not in {"4.3.02", "2.1.01.001"} for movement in result.movements)
    _assert_traces(result, entries)
    assert classify_banks(dataset, period) == result
    assert classify_banks(dataset, date(2026, 10, 1)) == ClosingPart()

    own_out = entries[24]
    own_in = entries[25]
    assert (own_out.source.ref, own_in.source.ref) == ("L12", "MRD2026090107")
    paired = classify_banks(replace(dataset, banks=(own_out, own_in)), period)
    assert paired.pending == () and [m.conta_codigo for m in paired.movements] == ["1.1.09.001"] * 2
    assert any("Par inequívoco" in item.regra and "MERIDIANO/MRD2026090107" in item.regra for item in paired.traces)
    duplicate_in = replace(own_in, source=SourceRef("meridiano.ofx", "MRD-DUPLICADO"))
    ambiguous = classify_banks(replace(dataset, banks=(own_out, own_in, duplicate_in)), period)
    assert len(ambiguous.pending) == 3
    assert all(item.tipo == "CONCILIAR_TRANSFERENCIA_PROPRIA" for item in ambiguous.pending)
    _assert_traces(ambiguous, (own_out, own_in, duplicate_in))
    different_day = replace(own_in, occurred_on=date(2026, 9, 3))
    unmatched = classify_banks(replace(dataset, banks=(own_out, different_day)), period)
    assert len(unmatched.pending) == 2
    _assert_traces(unmatched, (own_out, different_day))

    october_entry = replace(
        own_in, source=SourceRef("meridiano_outubro.ofx", "MRD2026100001"),
        occurred_on=date(2026, 10, 1), amount=Decimal("300000"),
    )
    october = classify_banks(replace(dataset, banks=entries + (october_entry,)), date(2026, 10, 1))
    assert len(october.movements) == 1
    assert october.movements[0].ref_origem == october_entry.source.ref
    assert october.movements[0].valor == Decimal("300000")
    _assert_traces(october, (october_entry,))

    salary = entries[1]
    salary_account = next(account for account in accounts if account.code == "2.1.04.001")
    for invalid_accounts in (
        tuple(account for account in accounts if account is not salary_account),
        tuple(replace(account, posting_allowed=False) if account is salary_account else account for account in accounts),
        tuple(replace(account, kind="SINTETICA") if account is salary_account else account for account in accounts),
        accounts + (salary_account,),
    ):
        blocked = classify_banks(replace(dataset, banks=(salary, entries[0]), accounts=invalid_accounts), period)
        assert blocked.movements[0].conta_codigo == "" and blocked.movements[0].confianca == 0
        assert blocked.movements[1].conta_codigo == "1.1.03.001"
        assert [(item.tipo, item.ref_origem) for item in blocked.pending] == [
            ("CONTA_NAO_LANCAVEL", "L15"), ("VALIDAR_RECONHECIMENTO_CONTABIL", "L9"),
        ]
        assert blocked.pending[0].valor_brl == salary.amount
        _assert_traces(blocked, (salary, entries[0]))

    unsupported_salary = replace(salary, description="PAGTO SALARIOS - FOLHA 09/2026")
    unknown = classify_banks(replace(dataset, banks=(unsupported_salary,)), period)
    assert unknown.movements[0].conta_codigo == "2.1.04.001" and unknown.movements[0].confianca == Decimal("0.80")
    assert [item.tipo for item in unknown.pending] == ["VALIDAR_RECONHECIMENTO_CONTABIL"]
    _assert_traces(unknown, (unsupported_salary,))
    unsupported_salary = replace(salary, description="FOLHA 09/2026 PAGTO SALARIOS")
    unknown = classify_banks(replace(dataset, banks=(unsupported_salary,)), period)
    assert unknown.movements[0].conta_codigo == "" and unknown.movements[0].confianca == 0
    assert [item.tipo for item in unknown.pending] == ["CLASSIFICACAO_BANCARIA"]
    _assert_traces(unknown, (unsupported_salary,))

    rounded = replace(entries[-1], amount=Decimal("-0.005"))
    rounded_result = classify_banks(replace(dataset, banks=(rounded,)), period)
    assert rounded_result.movements[0].valor == Decimal("-0.01")
    assert rounded_result.pending[0].valor_brl == Decimal("-0.01")
    _assert_traces(rounded_result, (rounded,))



if __name__ == "__main__":
    check()
