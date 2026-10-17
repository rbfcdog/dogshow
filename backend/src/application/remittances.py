from collections import defaultdict
from dataclasses import fields
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

# Taxas de câmbio (PTAX/Taxa Cliente) têm 4 casas; a planilha grava float e corta zeros à direita,
# então a precisão não pode ser lida do expoente do Decimal.
RATE_PRECISION = Decimal("0.0001")

from src.domain.closing import ClosingPart, Commission, Divergence, Invoice, Pending, trace
from src.domain.models import Dataset, RemittanceEntry
from src.domain.policies import ZERO, contract_candidates, in_period, money, output_id, period_key


def close_remittances(dataset: Dataset, period: date) -> ClosingPart:
    groups: dict[str, list[RemittanceEntry]] = defaultdict(list)
    for entry in dataset.remittances:
        groups[entry.source.ref].append(entry)
    economic_fields = tuple(field.name for field in fields(RemittanceEntry) if field.name not in {"source", "note"})
    contributions: dict[str, list[tuple[RemittanceEntry, Decimal]]] = defaultdict(list)
    names, divergences, pending, traces = {}, [], [], []
    terms = dataset.partner_terms
    share = terms.participation if terms else None
    for operation, occurrences in sorted(groups.items()):
        if not any(in_period(entry.contracted_on, period) for entry in occurrences):
            continue
        occurrences.sort(key=lambda entry: (entry.source.file, entry.source.sheet or "", entry.source.row or 0))
        entry = occurrences[0]
        if len({tuple(getattr(row, name) for name in economic_fields) for row in occurrences}) != 1:
            pending.append(Pending(output_id("REMESSA_CONFLITO", period, operation), "CONFLITO_OPERACAO_REMESSA",
                operation, None, "Ocorrências econômicas conflitantes: não há evidência para selecionar uma versão.", "Financeiro / Parceiro de câmbio"))
            continue
        for index, repeated in enumerate(occurrences[1:], 1):
            identifier = output_id("REMESSA_DUPLICADA", period, operation, str(index))
            amount = money(repeated.spread)
            pending.append(Pending(identifier, "CORRIGIR_RELATORIO_PONTE", operation, amount,
                "Repetição econômica idêntica contada uma vez por convenção técnica; validar reprocessamento. "
                "Valor é o spread da linha excedente, não previsão de desconto no repasse.", "Financeiro / Parceiro de câmbio"))
            traces.append(trace(identifier, repeated.source, amount, "Spread reportado na ocorrência excedente idêntica."))
        candidates = contract_candidates(dataset.contracts, entry.contracted_on, cnpj=entry.cnpj)
        contract = candidates[0] if len(candidates) == 1 else None
        rate = contract.remittance_rate if contract else None
        if rate is None:
            pending.append(Pending(output_id("REMESSA_CONTRATO", period, operation), "CONTRATO_REMESSA_NAO_RESOLVIDO",
                operation, None, "Contrato ausente/ambíguo ou spread vazio/padrão sem percentual explícito. "
                "Não inferir taxa padrão a partir das cobranças do parceiro.", "Comercial / Financeiro"))
        else:
            expected_rate = (entry.ptax_rate * (1 + rate)).quantize(RATE_PRECISION, rounding=ROUND_HALF_UP)
            difference = money(entry.foreign_amount * entry.ptax_rate * (entry.spread_rate - rate))
            if entry.customer_rate != expected_rate and difference != ZERO:
                identifier = output_id("REMESSA_DIVERGENCIA", period, operation)
                divergences.append(Divergence(identifier, entry.cnpj, "REMESSA", operation,
                    rate * 100, entry.spread_rate * 100, difference))
                rule = (f"Taxa Cliente {entry.customer_rate} difere de PTAX × (1 + spread da planilha) = {expected_rate} "
                    "na precisão de 4 casas; valor = ME × PTAX × (aplicado − planilha).")
                traces.append(trace(identifier, entry.source, difference, rule))
                pending_id = output_id("REMESSA_REVISAR_DIFERENCA", period, operation)
                pending.append(Pending(pending_id, "VALIDAR_DIVERGENCIA_REMESSA", operation, difference,
                    rule + " Conferir precisão, base e materialidade antes de contestar; nenhuma contestação presumida.", "Financeiro"))
                traces.append(trace(pending_id, entry.source, difference, rule))
        if entry.spread < ZERO:
            identifier = output_id("REMESSA_NEGATIVO", period, operation)
            amount = money(entry.spread)
            pending.append(Pending(identifier, "VALIDAR_SPREAD_NEGATIVO", operation, amount,
                "Spread negativo reportado. O pacote não determina exclusão, trava vencida ou obrigação de contestar. "
                "Projeção aplica participação ao realizado assinado; validar efeito antes de faturar.", "Financeiro"))
            traces.append(trace(identifier, entry.source, amount, "Spread negativo integral da fonte; política de tratamento ausente."))
        if share is None or not ZERO <= share <= Decimal("1"):
            pending.append(Pending(output_id("REMESSA_PARTICIPACAO", period, operation), "PARTICIPACAO_NAO_DOCUMENTADA",
                operation, None, "Percentual de participação não extraído inequivocamente do corpo do e-mail.", "Financeiro"))
            continue
        names[entry.cnpj] = contract.merchant if contract else entry.merchant
        contributions[entry.cnpj].append((entry, money(entry.spread * share)))
    commissions, invoices = [], []
    total = ZERO
    rule = f"Projeção: participação {share} do realizado assinado; percentual em {terms.source.file}/{terms.source.ref}; HALF_UP por operação." if terms else "Participação ausente."
    for taxpayer, entries in sorted(contributions.items()):
        identifier = output_id("REMESSA_COMISSAO", period, taxpayer)
        amount = sum((value for _, value in entries), ZERO)
        commissions.append(Commission(identifier, taxpayer, names[taxpayer], "REMESSA", amount))
        total += amount
        traces.extend(trace(identifier, entry.source, value, rule) for entry, value in entries)
    if contributions:
        first = next(iter(contributions.values()))[0][0]
        if total > ZERO and terms and terms.cnpj and terms.name:
            identifier = output_id("REMESSA_NF", period, terms.cnpj)
            invoices.append(Invoice(identifier, terms.cnpj, terms.name, "REMESSA", total,
                f"Proposta preliminar de intermediação — {period_key(period)}; validar ajustes e tratamento de negativos."))
            traces.extend(trace(identifier, entry.source, value, rule) for entries in contributions.values() for entry, value in entries)
        else:
            pending.append(Pending(output_id("REMESSA_TOMADOR", period), "VALIDAR_NOTA_REMESSA", first.source.ref,
                None, "Tomador fiscal não extraído inequivocamente do e-mail ou saldo não positivo.", "Financeiro"))
    return ClosingPart(commissions=tuple(commissions), divergences=tuple(divergences), invoices=tuple(invoices),
        pending=tuple(pending), traces=tuple(traces))
