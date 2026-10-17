from collections import Counter, defaultdict
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal, ROUND_DOWN
import re
from typing import Literal

from src.domain.models import (
    BankEntry,
    CheckoutEntry,
    Dataset,
    RemittanceEntry,
    SourceRef,
)
from src.domain.policies import ESTABLISHMENT_BINDINGS

ZERO = Decimal("0")
CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Literal["INFO", "WARNING", "ERROR"]
    message: str
    sources: tuple[SourceRef, ...]
    reported_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CheckoutGroup:
    kind: str
    rows: int
    gross: Decimal
    fees: Decimal
    net: Decimal
    sources: tuple[SourceRef, ...]


@dataclass(frozen=True, slots=True)
class CheckoutSummary:
    rows: int
    establishments: int
    reported_fees: Decimal
    fees_matching_truncation: int
    by_kind: tuple[CheckoutGroup, ...]
    unmatched_establishment_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RemittanceSummary:
    rows: int
    unique_operations: int
    reported_spread: Decimal
    negative_spread: Decimal
    status_counts: dict[str, int]
    settlements_outside_period: int
    sources: tuple[SourceRef, ...]


@dataclass(frozen=True, slots=True)
class BankSummary:
    bank: str
    rows: int
    incoming: Decimal
    outgoing: Decimal
    net: Decimal
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    sources: tuple[SourceRef, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    period: str
    status: Literal["DIAGNOSTIC_ONLY"]
    checkout: CheckoutSummary
    remittances: RemittanceSummary
    banks: tuple[BankSummary, ...]
    posting_accounts: int
    findings: tuple[Finding, ...]


def _in_period(value: date, period: date) -> bool:
    return (value.year, value.month) == (period.year, period.month)


def _checkout_summary(
    entries: list[CheckoutEntry], dataset: Dataset, findings: list[Finding]
) -> CheckoutSummary:
    contracts = {
        contract.establishment_code
        for contract in dataset.contracts
        if contract.establishment_code and "INATIVO" not in (contract.note or "").upper()
    }
    active_cnpjs = {
        contract.cnpj for contract in dataset.contracts
        if "INATIVO" not in (contract.note or "").upper()
    }
    contracts.update(code for code, cnpj in ESTABLISHMENT_BINDINGS.items() if cnpj in active_cnpjs)
    groups: dict[str, list[CheckoutEntry]] = defaultdict(list)
    by_id: dict[str, list[CheckoutEntry]] = defaultdict(list)
    all_ids = {entry.source.ref for entry in dataset.checkout}
    unmatched: dict[str, list[CheckoutEntry]] = defaultdict(list)
    missing_originals = []
    inconsistencies = []
    matching_truncation = 0
    for entry in entries:
        groups[entry.kind].append(entry)
        by_id[entry.source.ref].append(entry)
        if entry.establishment_code not in contracts:
            unmatched[entry.establishment_code].append(entry)
        if entry.original_ref and entry.original_ref not in all_ids:
            missing_originals.append(entry.source)
        if entry.gross - entry.fee != entry.net:
            inconsistencies.append(entry.source)
        truncated = (entry.gross * entry.applied_rate).quantize(CENT, rounding=ROUND_DOWN)
        matching_truncation += truncated == entry.fee
    for identifier, repeated in by_id.items():
        if len(repeated) > 1:
            findings.append(Finding(
                "DUPLICATE_CHECKOUT_ID", "ERROR", f"ID de checkout repetido: {identifier}.",
                tuple(entry.source for entry in repeated),
            ))
    for code, group in sorted(unmatched.items()):
        findings.append(Finding(
            "UNMATCHED_ESTABLISHMENT", "WARNING",
            f"EC {code} sem associação exata à tabela vigente; não inferir pelo nome.",
            tuple(entry.source for entry in group),
        ))
    if missing_originals:
        findings.append(Finding(
            "ORIGINAL_OUTSIDE_PACK", "INFO",
            "Origem fora do pacote; estorno é calculado pela taxa vigente na data original, quando determinável.",
            tuple(missing_originals),
        ))
    if inconsistencies:
        findings.append(Finding(
            "CHECKOUT_BALANCE_MISMATCH", "ERROR", "Bruto menos taxa difere do líquido.",
            tuple(inconsistencies),
        ))
    return CheckoutSummary(
        rows=len(entries),
        establishments=len({entry.establishment_code for entry in entries}),
        reported_fees=sum((entry.fee for entry in entries), ZERO),
        fees_matching_truncation=matching_truncation,
        by_kind=tuple(
            CheckoutGroup(
                kind, len(group), sum((entry.gross for entry in group), ZERO),
                sum((entry.fee for entry in group), ZERO),
                sum((entry.net for entry in group), ZERO),
                tuple(entry.source for entry in group),
            )
            for kind, group in sorted(groups.items())
        ),
        unmatched_establishment_codes=tuple(sorted(unmatched)),
    )


def _remittance_summary(
    entries: list[RemittanceEntry], dataset: Dataset, period: date, findings: list[Finding]
) -> RemittanceSummary:
    groups: dict[str, list[RemittanceEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.source.ref].append(entry)
    economic_fields = tuple(
        field.name for field in fields(RemittanceEntry) if field.name not in {"source", "note"}
    )
    for identifier, repeated in groups.items():
        if len(repeated) < 2:
            continue
        same_payload = all(
            all(getattr(entry, name) == getattr(repeated[0], name) for name in economic_fields)
            for entry in repeated[1:]
        )
        findings.append(Finding(
            "REPROCESSED_REMITTANCE_ID" if same_payload else "CONFLICTING_REMITTANCE_ID",
            "WARNING" if same_payload else "ERROR",
            f"Operação {identifier} repetida "
            + ("com mesmos dados econômicos; contar uma vez e solicitar correção do relatório."
               if same_payload else "com dados econômicos diferentes; bloquear o identificador até decisão."),
            tuple(entry.source for entry in repeated),
        ))
    negative = [entry for entry in entries if entry.spread < ZERO]
    outside = [
        entry for entry in entries
        if entry.settled_on is not None and not _in_period(entry.settled_on, period)
    ]
    unsettled = [entry for entry in entries if entry.settled_on is None]
    if negative:
        findings.append(Finding(
            "NEGATIVE_SPREAD", "WARNING",
            "Spread negativo excluído da comissão e da nota pela regra confirmada; contestar com a Ponte.",
            tuple(entry.source for entry in negative),
            sum((entry.spread for entry in negative), ZERO),
        ))
    if outside:
        findings.append(Finding(
            "SETTLEMENT_OUTSIDE_PERIOD", "INFO",
            "Liquidação em outro mês; a competência confirmada segue a data de contratação.",
            tuple(entry.source for entry in outside),
        ))
    if unsettled:
        findings.append(Finding(
            "MISSING_SETTLEMENT_DATE", "WARNING", "Operação sem data de liquidação.",
            tuple(entry.source for entry in unsettled),
        ))
    contracts_by_cnpj = defaultdict(list)
    for contract in dataset.contracts:
        if "INATIVO" not in (contract.note or "").upper():
            contracts_by_cnpj[contract.cnpj].append(contract)
    by_cnpj: dict[str, list[RemittanceEntry]] = defaultdict(list)
    for entry in entries:
        by_cnpj[entry.cnpj].append(entry)
    for cnpj, merchant_entries in sorted(by_cnpj.items()):
        contracts = contracts_by_cnpj.get(cnpj, [])
        if not contracts:
            findings.append(Finding(
                "UNDEFINED_REMITTANCE_CONTRACT", "WARNING",
                f"CNPJ {cnpj}: contrato não identificado; comparar taxas exige confirmação comercial.",
                tuple(entry.source for entry in merchant_entries)
                + tuple(contract.source for contract in contracts),
            ))
    return RemittanceSummary(
        rows=len(entries), unique_operations=len(groups),
        reported_spread=sum((entry.spread for entry in entries), ZERO),
        negative_spread=sum((entry.spread for entry in negative), ZERO),
        status_counts=dict(sorted(Counter(entry.status for entry in entries).items())),
        settlements_outside_period=len(outside),
        sources=tuple(entry.source for entry in entries),
    )


def _bank_summaries(
    entries: list[BankEntry], dataset: Dataset, period: date, findings: list[Finding]
) -> tuple[BankSummary, ...]:
    groups: dict[str, list[BankEntry]] = defaultdict(list)
    own_transfers: dict[tuple[date, Decimal], list[BankEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.bank].append(entry)
        description = entry.description.upper()
        if "TRANSF ENTRE CONTAS" in description or "MESMA TITULARIDADE" in description:
            own_transfers[(entry.occurred_on, abs(entry.amount))].append(entry)
        if entry.amount > ZERO and "SEM IDENTIFICACAO" in description:
            findings.append(Finding(
                "UNIDENTIFIED_CREDIT", "WARNING", "Crédito sem finalidade identificada.",
                (entry.source,), entry.amount,
            ))
        if "DEVOLUCAO PIX" in description:
            findings.append(Finding(
                "PIX_RETURN", "WARNING", "Repasse devolvido volta a ser obrigação; esclarecer o motivo do retorno.",
                (entry.source,), entry.amount,
            ))
        if "REPASSE COMISSAO" in description:
            match = re.search(r"\b(0[1-9]|1[0-2])/(\d{4})\b", description)
            if match and (int(match[2]), int(match[1])) != (period.year, period.month):
                findings.append(Finding(
                    "COMMISSION_OTHER_PERIOD", "INFO",
                    "Comissão de outra competência; o repasse de agosto já provisionado baixa o recebível.",
                    (entry.source,), entry.amount,
                ))
    for group in own_transfers.values():
        matched = (
            len(group) == 2
            and group[0].bank != group[1].bank
            and group[0].amount != ZERO
            and group[0].amount == -group[1].amount
        )
        if not matched:
            findings.append(Finding(
                "OWN_TRANSFER_UNRECONCILED", "WARNING",
                "Transferência própria sem par inequívoco por data e valor nos extratos do mês; "
                "verificar comprovante e eventual diferença de data.",
                tuple(entry.source for entry in group),
            ))
    summaries = []
    for bank, group in sorted(groups.items()):
        complete_file_period = all(
            _in_period(entry.occurred_on, period) for entry in dataset.banks if entry.bank == bank
        )
        opening = dataset.bank_opening_balances.get(bank) if complete_file_period else None
        closing = dataset.bank_closing_balances.get(bank) if complete_file_period else None
        net = sum((entry.amount for entry in group), ZERO)
        previous = opening
        inconsistent = []
        for entry in group:
            if entry.balance is not None:
                if previous is not None and previous + entry.amount != entry.balance:
                    inconsistent.append(entry.source)
                previous = entry.balance
            elif previous is not None:
                previous += entry.amount
        if inconsistent:
            findings.append(Finding(
                "BANK_RUNNING_BALANCE_MISMATCH", "ERROR", "Saldo bancário não encadeia com o movimento.",
                tuple(inconsistent),
            ))
        if opening is not None and closing is not None and opening + net != closing:
            findings.append(Finding(
                "BANK_CLOSING_BALANCE_MISMATCH", "ERROR",
                "Saldo inicial informado mais movimentos difere do saldo final informado.",
                tuple(entry.source for entry in group),
            ))
        summaries.append(BankSummary(
            bank=bank, rows=len(group),
            incoming=sum((entry.amount for entry in group if entry.amount > ZERO), ZERO),
            outgoing=-sum((entry.amount for entry in group if entry.amount < ZERO), ZERO),
            net=net, opening_balance=opening, closing_balance=closing,
            sources=tuple(entry.source for entry in group),
        ))
    return tuple(summaries)


def diagnose(dataset: Dataset, period: date) -> DiagnosticReport:
    if period.day != 1:
        raise ValueError("A competência deve ser representada pelo primeiro dia do mês.")
    findings: list[Finding] = []
    checkout = [entry for entry in dataset.checkout if _in_period(entry.occurred_on, period)]
    remittances = [entry for entry in dataset.remittances if _in_period(entry.contracted_on, period)]
    banks = [entry for entry in dataset.banks if _in_period(entry.occurred_on, period)]
    if not checkout and not remittances and not banks:
        findings.append(Finding(
            "NO_ROWS_IN_PERIOD", "INFO", "O pacote não contém movimentos para o mês solicitado.", (),
        ))
    return DiagnosticReport(
        period=f"{period.year:04d}-{period.month:02d}", status="DIAGNOSTIC_ONLY",
        checkout=_checkout_summary(checkout, dataset, findings),
        remittances=_remittance_summary(remittances, dataset, period, findings),
        banks=_bank_summaries(banks, dataset, period, findings),
        posting_accounts=sum(account.posting_allowed for account in dataset.accounts),
        findings=tuple(findings),
    )
