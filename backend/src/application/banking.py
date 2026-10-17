from collections import defaultdict
from datetime import date
from decimal import Decimal
from unicodedata import combining, normalize

from src.domain.closing import ClassifiedMovement, ClosingPart, Pending, trace
from src.domain.models import Account, BankEntry, Dataset
from src.domain.policies import in_period, money, output_id


def _description(entry: BankEntry) -> str:
    return " ".join(
        "".join(character for character in normalize("NFKD", entry.description) if not combining(character)).upper().split()
    )


def _classify(entry: BankEntry) -> tuple[str, Decimal, str]:
    description = _description(entry)
    exact = Decimal("1")
    assumed = Decimal("0.80")
    if "TRANSF ENTRE CONTAS" in description or "MESMA TITULARIDADE" in description:
        return "1.1.09.001", exact, "Transferência entre contas próprias; não representa receita ou despesa."
    if entry.amount > 0:
        if entry.bank == "AURORA":
            if description.startswith("TED RECEB") and "ORBE ADQUIRENCIA" in description:
                return "1.1.03.001", exact, "Liquidação Orbe baixa recebíveis; sem conciliação de parcelas ou igualdade com o líquido mensal."
            if description.startswith("DEVOLUCAO PIX") and "MUNDO ABERTO" in description:
                return "2.1.01.001", exact, "Devolução de repasse Mundo Aberto restabelece obrigação com o lojista; não é receita."
            if description.startswith("PIX RECEB") and "SEM IDENTIFICACAO" in description:
                return "1.1.09.002", assumed, "Premissa do time: usar Créditos não identificados até Helena identificar o PIX; não é receita."
        if entry.bank == "MERIDIANO":
            if "REPASSE COMISSAO 08/2026" in description and "PONTE CAMBIO" in description:
                return "1.1.03.002", exact, "Repasse Ponte de agosto já apropriado baixa comissão a receber; não é receita de setembro."
            if description.startswith("PIX RECEB") and "SEM IDENTIFICACAO" not in description:
                return "2.1.02.001", exact, "PIX de cliente no Meridiano representa recursos para remessa, não receita."
        if description.startswith("RESGATE"):
            return "1.1.02.001", exact, "Resgate de aplicação baixa o ativo; não inferir rendimento por diferença."
        if description.startswith("RENDIMENTO"):
            return "3.2.01", exact, "Rendimento explicitamente identificado no extrato."
    if entry.amount < 0:
        if entry.bank == "AURORA" and in_period(entry.occurred_on, date(2026, 9, 1)):
            for marker, code in (
                ("PAGTO SALARIOS - FOLHA 08/2026", "2.1.04.001"),
                ("GPS INSS 08/2026", "2.1.04.002"),
                ("GRF FGTS 08/2026", "2.1.04.003"),
                ("PAGTO ISS SAO PAULO DAMSP 08/2026", "2.1.03.003"),
                ("DEBITO DARF 0561 IRRF", "2.1.03.004"),
                ("DEBITO DARF 8109 PIS", "2.1.03.001"),
                ("DEBITO DARF 2172 COFINS", "2.1.03.002"),
            ):
                if description == marker:
                    return code, exact, "Folha ou tributo de agosto já provisionado: baixa do passivo, sem nova despesa em setembro."
        for marker, code in (
            ("TAR ", "4.2.01"),
            ("TARIFA ", "4.2.01"),
            ("BOLETO CONDOMINIO", "4.2.02"),
            ("ALUGUEL", "4.2.02"),
            ("DEB AUT AMAZON WEB SERVICES", "4.2.03"),
            ("CARTAO PJ - GOOGLE WORKSPACE", "4.2.04"),
            ("SEGURO EMPRESARIAL", "4.2.05"),
            ("PIX ENVIADO CONTABILIDADE EXATA", "4.2.06"),
            ("IOF S/ OPERACAO CREDITO", "4.3.01"),
        ):
            if description.startswith(marker):
                return code, exact, "Despesa identificada no extrato e não provisionada, conforme confirmação financeira."
        if description.startswith("APLICACAO"):
            return "1.1.02.001", exact, "Aplicação de principal constitui ativo financeiro, não despesa."
        if entry.bank == "MERIDIANO" and description.startswith("TED ENVIADA PONTE CAMBIO") and "LIQ OPERACOES" in description:
            return "2.1.02.001", exact, "Liquidação de operações na Ponte baixa recursos de clientes para remessas."
        if entry.bank == "AURORA" and description.startswith("PIX ENVIADO "):
            return "2.1.01.001", assumed, "Premissa do time: PIX enviado pelo Aurora, após fornecedores específicos, baixa repasse a lojista."
    return "", Decimal("0"), "Descrição sem regra evidenciada: contrapartida não identificada, sem presumir conta ou receita."


def classify_banks(dataset: Dataset, period: date) -> ClosingPart:
    entries = tuple(entry for entry in dataset.banks if in_period(entry.occurred_on, period))
    accounts: dict[str, list[Account]] = defaultdict(list)
    for account in dataset.accounts:
        accounts[account.code].append(account)
    classifications = tuple(_classify(entry) for entry in entries)
    transfers: dict[tuple[date, Decimal], list[BankEntry]] = defaultdict(list)
    for entry, (code, _, _) in zip(entries, classifications):
        if code == "1.1.09.001":
            transfers[(entry.occurred_on, abs(entry.amount))].append(entry)
    movements = []
    pending = []
    traces = []
    for entry, (code, confidence, rule) in zip(entries, classifications):
        proposed_code = code
        amount = money(entry.amount)
        identifier = output_id("movimento", period, entry.bank, entry.source.file, entry.source.ref)
        issues: list[tuple[str, str]] = []
        if not code:
            issues.append(("CLASSIFICACAO_BANCARIA", rule))
        else:
            candidates = accounts.get(code, [])
            if len(candidates) != 1 or candidates[0].kind != "ANALITICA" or not candidates[0].posting_allowed:
                issues.append((
                    "CONTA_NAO_LANCAVEL",
                    f"Contrapartida proposta {code} bloqueada: a conta deve existir de forma única, ser ANALITICA e aceitar lançamento. {rule}",
                ))
                code = ""
                confidence = Decimal("0")
                rule = issues[-1][1]
        if proposed_code == "1.1.09.002":
            issues.append((
                "IDENTIFICAR_PIX",
                "Identificar origem e finalidade do PIX recebido; valor é crédito bancário, não receita. Uso da transitória 1.1.09.002 é decisão justificada do time, não resposta literal de Helena.",
            ))
        if proposed_code == "2.1.01.001" and entry.amount > 0:
            issues.append((
                "MOTIVO_DEVOLUCAO_PIX",
                "Helena deve apurar o motivo do PIX de repasse Mundo Aberto que voltou; o valor restabelece obrigação com o lojista e não constitui receita.",
            ))
        if proposed_code == "1.1.09.001":
            group = transfers[(entry.occurred_on, abs(entry.amount))]
            matched = (
                len(group) == 2 and group[0].bank != group[1].bank
                and group[0].amount != 0 and group[0].amount == -group[1].amount
            )
            if matched:
                counterpart = group[1] if group[0] is entry else group[0]
                rule += f" Par inequívoco por data e valor: {counterpart.bank}/{counterpart.source.ref}."
            elif (
                entry.bank == "AURORA" and entry.source.ref == "L92"
                and entry.occurred_on == date(2026, 9, 30) and entry.amount == Decimal("-300000")
            ):
                issues.append((
                    "TRANSFERENCIA_EM_TRANSITO",
                    "Transferência própria em trânsito: saiu do Aurora em 30/09/2026 e crédito no Meridiano em 01/10/2026 foi confirmado por Helena. Conferir extrato de outubro quando disponível; não é erro, perda ou recrédito de setembro.",
                ))
            else:
                issues.append((
                    "CONCILIAR_TRANSFERENCIA_PROPRIA",
                    "Transferência própria sem par inequívoco entre bancos por data e valor no mês; conferir comprovante e eventual trânsito, sem inventar entrada, receita ou perda.",
                ))
        movements.append(ClassifiedMovement(
            identifier, entry.bank, entry.source.ref, entry.occurred_on.isoformat(),
            entry.description, amount, code, "REGRA", confidence,
        ))
        traces.append(trace(identifier, entry.source, amount, rule))
        for kind, reason in issues:
            pending_id = output_id("pendencia", period, kind, entry.bank, entry.source.file, entry.source.ref)
            pending.append(Pending(pending_id, kind, entry.source.ref, amount, reason, "Helena"))
            traces.append(trace(pending_id, entry.source, amount, reason))
    return ClosingPart(movements=tuple(movements), pending=tuple(pending), traces=tuple(traces))
