from collections import defaultdict
from dataclasses import fields
from datetime import date
from decimal import Decimal

from src.domain.closing import ClosingPart, Commission, Divergence, Invoice, Pending, trace
from src.domain.models import Dataset, RemittanceEntry
from src.domain.policies import (
    DEFAULT_REMITTANCE_RATE,
    PONTE_CNPJ,
    PONTE_NAME,
    REMITTANCE_RATE_TOLERANCE,
    ZERO,
    contract_candidates,
    in_period,
    money,
    output_id,
    period_key,
)


def close_remittances(dataset: Dataset, period: date) -> ClosingPart:
    groups: dict[str, list[RemittanceEntry]] = defaultdict(list)
    for entry in dataset.remittances:
        groups[entry.source.ref].append(entry)
    economic_fields = tuple(
        field.name for field in fields(RemittanceEntry) if field.name not in {"source", "note"}
    )
    contributions: dict[str, list[tuple[RemittanceEntry, Decimal]]] = defaultdict(list)
    names: dict[str, str] = {}
    divergences = []
    pending = []
    traces = []
    for operation, occurrences in sorted(groups.items()):
        if not any(in_period(entry.contracted_on, period) for entry in occurrences):
            continue
        occurrences.sort(key=lambda entry: (
            entry.source.file, entry.source.sheet or "", entry.source.row or 0,
        ))
        entry = occurrences[0]
        payloads = {tuple(getattr(row, name) for name in economic_fields) for row in occurrences}
        if len(payloads) != 1:
            locations = "; ".join(
                f"{row.source.file}/{row.source.sheet or ''}/linha {row.source.row}"
                for row in occurrences
            )
            pending.append(Pending(
                output_id("REMESSA_CONFLITO", period, operation),
                "CONFLITO_OPERACAO_REMESSA", operation, None,
                f"Operação {operation} bloqueada: ocorrências A:O conflitantes em {locations}. "
                "Não há política confirmada para escolher uma ocorrência; corrigir com a Ponte.",
                "Helena / Ponte Câmbio",
            ))
            continue
        for index, repeated in enumerate(occurrences[1:], 1):
            identifier = output_id("REMESSA_DUPLICADA", period, operation, str(index))
            amount = money(repeated.spread)
            pending.append(Pending(
                identifier, "CORRIGIR_RELATORIO_PONTE", operation, amount,
                f"Operação {operation} contada uma vez; solicitar correção da ocorrência excedente "
                f"na linha {repeated.source.row}. Valor é o Spread R$ integral repetido, assinado "
                "como no relatório, não participação da Tarvo nem previsão de desconto no TED. "
                "Contestação ainda não aberta; considerar dentro do prazo.",
                "Helena",
            ))
            traces.append(trace(identifier, repeated.source, amount, "Spread realizado da ocorrência excedente A:O idêntica; corrigir relatório Ponte"))
        candidates = contract_candidates(dataset.contracts, entry.contracted_on, cnpj=entry.cnpj)
        contract = candidates[0] if len(candidates) == 1 else None
        if contract is None:
            pending.append(Pending(
                output_id("REMESSA_CONTRATO", period, operation),
                "CONTRATO_REMESSA_NAO_RESOLVIDO", operation, None,
                f"CNPJ {entry.cnpj}, operação {operation}: "
                f"{'contrato ausente' if not candidates else 'contrato ambíguo'} na data de contratação "
                f"{entry.contracted_on.isoformat()}. Confirmar taxa para comparar o contrato; "
                "a participação no realizado conhecido não é bloqueada por isso.",
                "Helena / Comercial",
            ))
        difference = None
        if contract is not None:
            contracted_rate = contract.remittance_rate
            if contracted_rate is None:
                contracted_rate = DEFAULT_REMITTANCE_RATE
            delta_rate = entry.spread_rate - contracted_rate
            if abs(delta_rate) >= REMITTANCE_RATE_TOLERANCE:
                difference = money(entry.foreign_amount * entry.ptax_rate * delta_rate)
                identifier = output_id("REMESSA_DIVERGENCIA", period, operation)
                divergences.append(Divergence(
                    identifier, entry.cnpj, "REMESSA", operation,
                    contracted_rate * 100, entry.spread_rate * 100, difference,
                ))
                traces.append(trace(identifier, entry.source, difference, "Valor ME × PTAX × (spread aplicado − contratado), integral assinado, sem participação de 50%"))
        if entry.spread < ZERO or difference is not None:
            identifier = output_id("REMESSA_CONTESTACAO", period, operation)
            amount = difference if difference is not None else money(entry.spread)
            basis = (
                "diferença cambial integral assinada = Valor ME × PTAX × (taxa aplicada − contratada), "
                "positiva para excesso cobrado e negativa para valor abaixo do contrato; não é participação de 50%"
                if difference is not None else
                "Spread R$ realizado integral negativo, perda reportada; diferença contra contrato não determinada"
            )
            negative_rule = (
                " Spread negativo de trava vencida: excluído da comissão e da NF, sem usar spread estimado."
                if entry.spread < ZERO else
                " O realizado positivo permanece na comissão e na NF; eventual ajuste só no mês em que ocorrer."
            )
            pending.append(Pending(
                identifier, "CONTESTAR_REMESSA_PONTE", operation, amount,
                f"Helena deve contestar a operação {operation}; valor representa {basis}."
                f"{negative_rule} Contestação ainda não aberta; considerar dentro do prazo. "
                "Não pressupor desconto no TED futuro.",
                "Helena",
            ))
            traces.append(trace(identifier, entry.source, amount, basis))
        if entry.spread <= ZERO:
            continue
        names[entry.cnpj] = contract.merchant if contract is not None else entry.merchant
        contributions[entry.cnpj].append((entry, money(entry.spread * Decimal("0.50"))))
    commissions = []
    total = ZERO
    for cnpj, entries in sorted(contributions.items()):
        identifier = output_id("REMESSA_COMISSAO", period, cnpj)
        amount = sum((value for _, value in entries), ZERO)
        commissions.append(Commission(identifier, cnpj, names[cnpj], "REMESSA", amount))
        total += amount
        traces.extend(
            trace(identifier, entry.source, value, "50% do Spread R$ realizado positivo; HALF_UP por operação; sem abater contestação")
            for entry, value in entries
        )
    invoices = []
    if total > ZERO:
        identifier = output_id("REMESSA_NF", period, PONTE_CNPJ)
        invoices.append(Invoice(
            identifier, PONTE_CNPJ, PONTE_NAME, "REMESSA", total,
            f"Intermediação de remessas cambiais — competência {period_key(period)}.",
        ))
        receipt_id = output_id("REMESSA_CONFERIR_REPASSE", period, PONTE_CNPJ)
        first_entry = next(iter(contributions.values()))[0][0]
        pending.append(Pending(
            receipt_id, "CONFERIR_REPASSE_FUTURO_PONTE", first_entry.source.ref, total,
            "Helena deve conferir o repasse quando recebido. Valor é o total esperado da NF de participação "
            "no spread realizado elegível, não previsão de TED líquido. Não pressupor descontos de perdas, "
            "duplicata ou contestações; estas ainda não foram abertas e estão dentro do prazo.",
            "Helena",
        ))
        for entries in contributions.values():
            for entry, value in entries:
                traces.append(trace(identifier, entry.source, value, "Participação de 50% arredondada por operação na NF única para Ponte"))
                traces.append(trace(receipt_id, entry.source, value, "Total esperado da NF, não previsão do TED líquido"))
    return ClosingPart(
        commissions=tuple(commissions), divergences=tuple(divergences), invoices=tuple(invoices),
        pending=tuple(pending), traces=tuple(traces),
    )
