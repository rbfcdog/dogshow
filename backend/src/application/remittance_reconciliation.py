import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

from src.domain.closing import ClosingPart, Pending, Trace, trace
from src.domain.models import BankEntry, Dataset, RemittanceEntry, SourceRef
from src.domain.policies import ZERO, in_period, money, output_id, period_key
from src.infrastructure.normalization_sources import normalize_name
from src.infrastructure.parsing import cnpj


_CNPJ = re.compile(r"(?<![0-9])(?:[0-9]{2}\.[0-9]{3}\.[0-9]{3}/[0-9]{4}-[0-9]{2}|[0-9]{14})(?![0-9])")


def _source_key(source: SourceRef) -> tuple[str, str, int, str]:
    return source.file, source.sheet or "", source.row if source.row is not None else -1, source.ref


def _location(source: SourceRef) -> str:
    return f"{source.file}/{source.sheet or ''}/linha {source.row}/ref {source.ref}"


def reconcile_remittance_resources(
    dataset: Dataset, period: date, operations: tuple[RemittanceEntry, ...],
) -> ClosingPart:
    operations = tuple(sorted(
        (entry for entry in operations if in_period(entry.contracted_on, period)),
        key=lambda entry: _source_key(entry.source),
    ))
    banks = sorted(
        (entry for entry in dataset.banks if entry.bank == "MERIDIANO" and in_period(entry.occurred_on, period)),
        key=lambda entry: (_source_key(entry.source), entry.occurred_on, entry.description, entry.amount),
    )
    daily_operations: dict[date, list[RemittanceEntry]] = defaultdict(list)
    client_operations: dict[str, list[RemittanceEntry]] = defaultdict(list)
    names: dict[str, set[str]] = defaultdict(set)
    for entry in operations:
        daily_operations[entry.contracted_on].append(entry)
        client_operations[entry.cnpj].append(entry)
        names[entry.cnpj].add(normalize_name(entry.merchant))
    known_cnpjs = set(client_operations) | {contract.cnpj for contract in dataset.contracts}
    for contract in dataset.contracts:
        if contract.cnpj in client_operations:
            names[contract.cnpj].add(normalize_name(contract.merchant))
    daily_teds: dict[date, list[BankEntry]] = defaultdict(list)
    pix_entries = []
    for entry in banks:
        description = normalize_name(entry.description)
        if entry.amount < ZERO and description.startswith("ted enviada ponte ") and " liq operacoes" in description:
            daily_teds[entry.occurred_on].append(entry)
        if entry.amount > ZERO and re.match(r"^pix receb(?:ido|ida)?(?: |$)", description):
            pix_entries.append(entry)
    raw_ids = {entry.source.ref for entry in dataset.remittances if in_period(entry.contracted_on, period)}
    received_ids = {entry.source.ref for entry in operations}
    pending: list[Pending] = []
    traces: list[Trace] = []
    incomplete_kind = "CONFERENCIA_RECURSOS_REMESSA_INCOMPLETA"

    def incomplete(scope: str, reference: str, reason: str) -> None:
        pending.append(Pending(
            output_id(incomplete_kind, period, "MERIDIANO", scope),
            incomplete_kind, reference, None, reason, "Financeiro / Ponte Câmbio",
        ))

    def difference(
        kind: str, scope: str, reference: str, contributions: list[tuple[SourceRef, Decimal, str]], reason: str,
    ) -> None:
        amount = money(sum((value for _, value, _ in contributions), ZERO))
        if amount == ZERO:
            return
        identifier = output_id(kind, period, scope)
        ordered = sorted(contributions, key=lambda item: (_source_key(item[0]), item[1], item[2]))
        locations = "; ".join(_location(source) for source, _, _ in ordered)
        pending.append(Pending(
            identifier, kind, reference, amount,
            f"{reason} Referências: {locations}. Conferência investigativa de fluxos, sem alterar "
            "cadastro, comissão, NF ou classificação bancária. Não somar diferenças diárias e mensais "
            "como exposição: podem refletir os mesmos fluxos.",
            "Financeiro / Ponte Câmbio",
        ))
        traces.extend(trace(identifier, source, value, rule) for source, value, rule in ordered)

    if raw_ids != received_ids:
        missing = ", ".join(sorted(raw_ids - received_ids)) or "nenhum"
        extra = ", ".join(sorted(received_ids - raw_ids)) or "nenhum"
        incomplete(
            "OPERACOES", min(raw_ids | received_ids),
            f"Conferência de recursos Meridiano de {period_key(period)} indisponível: cobertura de operações "
            f"incompleta. IDs brutos não recebidos: {missing}; IDs recebidos sem origem no mês: {extra}. "
            "Resolver operações conflitantes/ausentes antes de comparar os recursos; não assumir caixa zero "
            "nem calcular diferenças financeiras sobre base truncada. Comissão e NF são independentes.",
        )
        return ClosingPart(pending=tuple(pending))
    if not operations and not daily_teds and not pix_entries:
        return ClosingPart()
    if operations and not banks:
        incomplete(
            "FONTE", operations[0].source.ref,
            f"Sem movimentos Meridiano de {period_key(period)} disponíveis para conferir as operações. "
            "Solicitar fonte bancária do período; ausência de dados não é caixa zero. Conferência diária "
            "e mensal indisponível, sem inferir falta de recursos ou alterar comissão/NF.",
        )
        return ClosingPart(pending=tuple(pending))

    for day in sorted(set(daily_operations) | set(daily_teds)):
        entries = daily_operations[day]
        teds = daily_teds[day]
        contributions = [
            (entry.source, -money(entry.amount), "TED enviado positivo = -valor bancário; agregado por data")
            for entry in teds
        ] + [
            (entry.source, -money(entry.brl_amount + entry.iof), "-(principal + IOF) da operação única por contratação")
            for entry in entries
        ]
        difference(
            "LIQUIDACAO_REMESSA_DIVERGENTE", day.isoformat(),
            entries[0].source.ref if entries else teds[0].source.ref, contributions,
            f"Meridiano, {day.isoformat()}: diferença assinada = TED enviado positivo menos (principal + IOF) "
            "das operações únicas por data de contratação. Critério diário agregado, não vinculação documental "
            "individual; solicitar conferência das fontes, sem afirmar falta de funding, perda ou desconto de comissão.",
        )

    client_pix: dict[str, list[tuple[BankEntry, str]]] = defaultdict(list)
    unresolved = []
    for entry in pix_entries:
        identifiers = {cnpj(match.group(), entry.source) for match in _CNPJ.finditer(entry.description)}
        if identifiers:
            candidates = identifiers & known_cnpjs if len(identifiers) == 1 else set()
            basis = f"CNPJ explícito na descrição: {', '.join(sorted(identifiers))}"
        else:
            nominal = re.sub(r"^pix receb(?:ido|ida)?\s+", "", normalize_name(entry.description))
            matches = {
                identifier: sorted(name for name in alternatives if len(name.split()) >= 2 and (
                    name == nominal or name.startswith(nominal) or nominal.startswith(name)
                ))
                for identifier, alternatives in sorted(names.items())
                if len(nominal.split()) >= 2
            }
            candidates = {identifier for identifier, alternatives in matches.items() if alternatives}
            evidence = "; ".join(
                f"{identifier}: {', '.join(matches[identifier])}" for identifier in sorted(candidates)
            ) or "nenhum nome completo correspondente"
            basis = (
                f"base nominal normalizada '{nominal}', igualdade/prefixo do nome completo em ambos os sentidos, "
                f"mínimo dois termos e CNPJ único; candidatos: {evidence}"
            )
        if len(candidates) != 1:
            kind = "CLIENTE_RECURSO_REMESSA_NAO_RESOLVIDO"
            identifier = output_id(kind, period, entry.bank, entry.source.file, entry.source.ref)
            amount = money(entry.amount)
            reason = (
                f"PIX recebido em {_location(entry.source)} sem correspondência de cliente única; {basis}. "
                "Valor observado no banco, não distribuído. Solicitar identificação; hipótese de conferência "
                "não altera cadastro, comissão, NF ou classificação bancária. Conferência mensal suspensa "
                "para evitar diferenças falsas."
            )
            pending.append(Pending(identifier, kind, entry.source.ref, amount, reason, "Financeiro / Ponte Câmbio"))
            traces.append(trace(identifier, entry.source, amount, reason))
            unresolved.append(entry)
        else:
            client_pix[next(iter(candidates))].append((entry, basis))
    if unresolved:
        incomplete(
            "MENSAL", unresolved[0].source.ref,
            "Conferência mensal de recursos Meridiano incompleta por PIX sem cliente único: "
            + "; ".join(_location(entry.source) for entry in unresolved)
            + ". Não calcular diferenças mensais por cliente nem distribuir PIX ambíguo/desconhecido. "
            "Conferência diária independente mantida; comissão e NF inalteradas.",
        )
    else:
        for identifier in sorted(set(client_operations) | set(client_pix)):
            entries = client_operations[identifier]
            receipts = client_pix[identifier]
            contributions = [
                (entry.source, money(entry.amount), f"PIX recebido positivo; hipótese determinística: {basis}")
                for entry, basis in receipts
            ] + [
                (entry.source, -money(entry.brl_amount + entry.iof), "-(principal + IOF) da operação única do CNPJ")
                for entry in entries
            ]
            nominal_basis = "; ".join(f"{entry.source.ref}: {basis}" for entry, basis in receipts) or "sem PIX identificado"
            difference(
                "RECURSOS_REMESSA_DIVERGENTES", identifier,
                entries[0].source.ref if entries else receipts[0][0].source.ref, contributions,
                f"CNPJ {identifier}, Meridiano {period_key(period)}: DIFERENÇA DOS FLUXOS IDENTIFICADOS NO RECORTE "
                "= PIX recebido menos (principal + IOF) das operações únicas. Não pressupõe saldo inicial zero "
                "nem constitui saldo contábil, recebível ou receita. Apurar saldo anterior, adiantamento, operação "
                f"ausente ou devolução, sem concluir devolução automática. Identificação: {nominal_basis}.",
            )
    return ClosingPart(pending=tuple(pending), traces=tuple(traces))
