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
        return "1.1.09.001", exact, "Descrição indica transferência entre contas próprias; não representa receita ou despesa."
    if entry.amount > 0:
        if entry.bank == "AURORA":
            if description.startswith("TED RECEB") and "ORBE ADQUIRENCIA" in description:
                return "1.1.03.001", assumed, "Inferência pela descrição e catálogo: possível baixa de recebível Orbe; reconhecimento anterior não comprovado no pacote."
            if description.startswith("DEVOLUCAO PIX"):
                return "", Decimal("0"), "Devolução identificada, mas origem, motivo e obrigação anterior não comprovados no pacote."
            if description.startswith("PIX RECEB") and "SEM IDENTIFICACAO" in description:
                return "1.1.09.002", assumed, "Proposta de conta transitória existente para crédito não identificado; validar finalidade e classificação."
        if entry.bank == "MERIDIANO":
            if "REPASSE COMISSAO" in description:
                return "1.1.03.002", assumed, "Possível baixa de comissão de câmbio a receber; apropriação anterior não consta no pacote."
            if description.startswith("PIX RECEB"):
                return "", Decimal("0"), "PIX recebido: o pacote não comprova a finalidade do recurso (remessa ou outra)."
        if description.startswith("RESGATE"):
            return "1.1.02.001", exact, "Resgate de aplicação baixa o ativo; não inferir rendimento por diferença."
        if description.startswith("RENDIMENTO"):
            return "3.2.01", exact, "Rendimento explicitamente identificado no extrato."
    if entry.amount < 0:
        for marker, code in (
            ("PAGTO SALARIOS", "2.1.04.001"), ("GPS INSS", "2.1.04.002"),
            ("GRF FGTS", "2.1.04.003"), ("PAGTO ISS", "2.1.03.003"),
            ("DEBITO DARF 0561 IRRF", "2.1.03.004"), ("DEBITO DARF 8109 PIS", "2.1.03.001"),
            ("DEBITO DARF 2172 COFINS", "2.1.03.002"),
        ):
            if description.startswith(marker):
                return code, assumed, "Proposta de baixa de obrigação pela descrição; o pacote não comprova provisão anterior."
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
                return code, assumed, "Categoria inferida pela descrição; validar despesa ou baixa de obrigação, sem provisão comprovada no pacote."
        if description.startswith("APLICACAO"):
            return "1.1.02.001", exact, "Aplicação de principal constitui ativo financeiro, não despesa."
        if entry.bank == "MERIDIANO" and description.startswith("TED ENVIADA PONTE CAMBIO") and "LIQ OPERACOES" in description:
            return "2.1.02.001", assumed, "Possível liquidação de remessas indicada no texto; validar origem dos recursos e contrapartida."
        if entry.bank == "AURORA" and description.startswith("PIX ENVIADO "):
            return "", Decimal("0"), "PIX enviado sem documento no pacote que comprove repasse ou despesa; finalidade pendente."
    return "", Decimal("0"), "Descrição sem regra evidenciada no pacote: contrapartida não identificada, sem presumir conta ou receita."


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
            elif confidence < Decimal("1"):
                issues.append(("VALIDAR_RECONHECIMENTO_CONTABIL", rule))
        if proposed_code == "1.1.09.002":
            issues.append((
                "IDENTIFICAR_PIX",
                "Identificar origem e finalidade do PIX; conta transitória é proposta técnica, não receita comprovada.",
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
            else:
                issues.append((
                    "CONCILIAR_TRANSFERENCIA_PROPRIA",
                    "Transferência própria sem par inequívoco entre os extratos do pacote por data e valor; conferir comprovante e eventual trânsito, sem inventar entrada, receita ou perda.",
                ))
        movements.append(ClassifiedMovement(
            identifier, entry.bank, entry.source.ref, entry.occurred_on.isoformat(),
            entry.description, amount, code, "REGRA", confidence,
        ))
        traces.append(trace(identifier, entry.source, amount, rule))
        for kind, reason in issues:
            pending_id = output_id("pendencia", period, kind, entry.bank, entry.source.file, entry.source.ref)
            pending.append(Pending(pending_id, kind, entry.source.ref, amount, reason, "Financeiro"))
            traces.append(trace(pending_id, entry.source, amount, reason))
    return ClosingPart(movements=tuple(movements), pending=tuple(pending), traces=tuple(traces))
