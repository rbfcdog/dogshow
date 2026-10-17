from collections import defaultdict
from datetime import date
from decimal import Decimal

from src.domain.closing import (
    ClosingPart,
    Commission,
    Divergence,
    Invoice,
    Pending,
    trace,
)
from src.domain.models import CheckoutEntry, DataPackError, Dataset
from src.domain.policies import (
    ZERO,
    has_commercial_question,
    contract_candidates,
    in_period,
    money,
    output_id,
    period_key,
)


def _rate_key(entry: CheckoutEntry) -> str:
    if entry.kind in {"ANTECIPACAO", "REVERSAO ANTECIPACAO"}:
        return "ANTECIPACAO"
    if entry.modality in {"DEBITO", "CREDITO A VISTA", "PIX"}:
        return entry.modality
    if entry.modality == "CREDITO PARCELADO" and entry.installments is not None:
        if 2 <= entry.installments <= 6:
            return "CREDITO 2-6X"
        if 7 <= entry.installments <= 12:
            return "CREDITO 7-12X"
    raise ValueError("Modalidade ou quantidade de parcelas não determina faixa contratual.")


def _reference(entry: CheckoutEntry, originals: dict[str, CheckoutEntry]) -> CheckoutEntry:
    if entry.kind in {"VENDA", "ANTECIPACAO"}:
        if entry.gross < ZERO:
            raise ValueError("Venda ou antecipação com bruto negativo; evento bloqueado.")
        return entry
    if entry.kind not in {"ESTORNO", "REVERSAO ANTECIPACAO"}:
        raise ValueError("Tipo de lançamento sem regra confirmada; evento bloqueado.")
    if entry.gross > ZERO:
        raise ValueError("Estorno ou reversão com bruto positivo; evento bloqueado.")
    original = originals.get(entry.original_ref or "")
    if original is None:
        if entry.kind == "REVERSAO ANTECIPACAO":
            raise ValueError("Antecipação referenciada ausente; não é possível comprovar anulação integral.")
        if entry.original_on is None or entry.original_on > entry.occurred_on:
            raise ValueError("Venda original ausente e data histórica ausente ou inconsistente.")
        _rate_key(entry)
        return entry
    expected = "VENDA" if entry.kind == "ESTORNO" else "ANTECIPACAO"
    if original.kind != expected or original.establishment_code != entry.establishment_code:
        raise ValueError("Referência não corresponde ao tipo e EC do lançamento original.")
    if original.occurred_on > entry.occurred_on or (
        entry.original_on is not None and entry.original_on != original.occurred_on
    ):
        raise ValueError("Data da referência original inconsistente com o movimento.")
    if original.gross <= ZERO or -entry.gross > original.gross:
        raise ValueError("Bruto do estorno ou reversão excede ou contradiz o lançamento original.")
    if entry.kind == "REVERSAO ANTECIPACAO" and entry.gross != -original.gross:
        raise ValueError("Reversão parcial não autorizada: a regra só anula integralmente a antecipação original.")
    return original


def close_checkout(dataset: Dataset, period: date) -> ClosingPart:
    originals: dict[str, CheckoutEntry] = {}
    reversed_refs: set[str] = set()
    for entry in dataset.checkout:
        if entry.source.ref in originals:
            raise DataPackError("ID de checkout repetido; deduplicação não autorizada.", entry.source)
        originals[entry.source.ref] = entry
        if entry.kind == "REVERSAO ANTECIPACAO" and entry.original_ref:
            if entry.original_ref in reversed_refs:
                raise DataPackError("Antecipação referenciada por mais de uma reversão.", entry.source)
            reversed_refs.add(entry.original_ref)
    contracts = tuple(contract for contract in dataset.contracts if "2025" not in (contract.source.sheet or ""))
    contributions: dict[str, list[tuple[CheckoutEntry, Decimal, str]]] = defaultdict(list)
    names: dict[str, tuple[date, str]] = {}
    divergences = []
    pending = []
    traces = []
    commercial_questions: set[str] = set()
    entries = sorted(
        (entry for entry in dataset.checkout if in_period(entry.occurred_on, period)),
        key=lambda entry: (entry.occurred_on, entry.source.ref),
    )
    for entry in entries:
        try:
            reference = _reference(entry, originals)
            reference_date = reference.occurred_on if reference is not entry or entry.kind in {
                "VENDA", "ANTECIPACAO"
            } else entry.original_on
            if reference_date is None:
                raise ValueError("Data histórica indeterminada.")
            candidates = contract_candidates(
                contracts, reference_date, establishment_code=entry.establishment_code,
            )
            if len(candidates) != 1:
                raise ValueError(
                    f"EC {entry.establishment_code}: encontrados {len(candidates)} contratos vigentes "
                    f"em {reference_date.isoformat()}; Comercial deve confirmar contrato e taxa."
                )
            contract = candidates[0]
            key = _rate_key(reference)
            if key not in contract.rates:
                raise ValueError(f"Contrato sem taxa histórica para {key}; Comercial deve confirmar.")
            rate = contract.rates[key]
            if has_commercial_question(contract):
                commercial_questions.add(contract.cnpj)
        except ValueError as error:
            identifier = output_id("pendencia-checkout-bloqueado", period, entry.source.ref)
            reason = f"{error} Valor informado é a taxa reportada pela Orbe, não comissão apurada. Sem faturamento deste evento."
            pending.append(Pending(
                identifier, "CHECKOUT_BLOQUEADO", entry.source.ref, entry.fee, reason,
                "Comercial" if "contrato" in str(error).lower() else "Financeiro",
            ))
            traces.append(trace(identifier, entry.source, entry.fee, "Taxa reportada; comissão indeterminada: " + str(error)))
            continue
        amount = money(entry.gross * rate)
        rule = (
            f"Apuração preliminar: {entry.kind}; bruto × taxa da planilha {rate}; convenção técnica HALF_UP por linha; "
            f"faixa {key}; data original {reference_date.isoformat()}; "
            f"contrato {contract.source.file}/{contract.source.sheet or ''}/{contract.source.ref}; "
            f"referência {entry.original_ref or reference.source.ref}; "
            f"campos históricos {reference.source.file}/{reference.source.ref}"
        )
        contributions[contract.cnpj].append((entry, amount, rule))
        nominal = (reference_date, contract.merchant)
        if contract.cnpj not in names or nominal > names[contract.cnpj]:
            names[contract.cnpj] = nominal
        if entry.applied_rate != rate:
            difference = money(entry.gross * (entry.applied_rate - rate))
            identifier = output_id("divergencia-checkout", period, entry.source.ref)
            divergences.append(Divergence(
                identifier, contract.cnpj, "CHECKOUT", entry.source.ref,
                rate * 100, entry.applied_rate * 100, difference,
            ))
            difference_rule = "Bruto assinado × (taxa aplicada − contratada); HALF_UP; " + rule
            traces.append(trace(identifier, entry.source, difference, difference_rule))
            pending_id = output_id("pendencia-checkout-divergencia", period, entry.source.ref)
            reason = (
                f"Transação {entry.source.ref}: diferença contratual assinada calculada pelo bruto × "
                "(taxa aplicada − contratada), não por taxa reportada menos comissão. "
                "Revisar cobrança e eventual devolução manual; nenhuma devolução ou contestação executada."
            )
            pending.append(Pending(
                pending_id, "DIVERGENCIA_CHECKOUT", entry.source.ref, difference, reason,
                "Financeiro / Comercial",
            ))
            traces.append(trace(pending_id, entry.source, difference, difference_rule))
    commissions = []
    invoices = []
    for cnpj, rows in sorted(contributions.items()):
        total = sum((amount for _, amount, _ in rows), ZERO)
        merchant = names[cnpj][1]
        identifier = output_id("comissao-checkout", period, cnpj)
        commissions.append(Commission(identifier, cnpj, merchant, "CHECKOUT", total))
        traces.extend(trace(identifier, entry.source, amount, rule) for entry, amount, rule in rows)
        if cnpj in commercial_questions:
            pending.append(Pending(output_id("condicao-comercial", period, cnpj), "CONDICAO_COMERCIAL_NAO_DEFINIDA",
                rows[0][0].source.ref, None,
                "Observação/comentário da planilha menciona condição comercial não detalhada. "
                "Cálculo preliminar usa taxas preenchidas; não há evidência de isenção. Nota bloqueada até esclarecer a condição.",
                "Comercial"))
        elif total > ZERO:
            invoice_id = output_id("nota-checkout", period, cnpj)
            invoices.append(Invoice(
                invoice_id, cnpj, merchant, "CHECKOUT", total,
                f"Proposta preliminar: checkout, MDR e antecipação — competência {period_key(period)}; validar política fiscal.",
            ))
            traces.extend(trace(invoice_id, entry.source, amount, rule) for entry, amount, rule in rows)
        if total < ZERO:
            pending_id = output_id("pendencia-checkout-negativo", period, cnpj)
            pending.append(Pending(
                pending_id, "SALDO_NEGATIVO_CHECKOUT", rows[0][0].source.ref, total,
                f"Saldo agregado negativo do CNPJ {cnpj}: o pacote não define emissão fiscal ou transporte; validar tratamento.",
                "Financeiro",
            ))
            traces.extend(trace(pending_id, entry.source, amount, rule) for entry, amount, rule in rows)
    return ClosingPart(
        commissions=tuple(commissions), divergences=tuple(divergences), invoices=tuple(invoices),
        pending=tuple(pending), traces=tuple(traces),
    )
