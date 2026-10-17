from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class SourceRef:
    file: str
    ref: str
    row: int | None = None
    sheet: str | None = None


class DataPackError(ValueError):
    def __init__(self, message: str, source: SourceRef | None = None) -> None:
        super().__init__(message)
        self.source = source


@dataclass(frozen=True, slots=True)
class CheckoutEntry:
    source: SourceRef
    occurred_on: date
    establishment_code: str
    merchant: str
    kind: str
    modality: str
    installments: int | None
    gross: Decimal
    applied_rate: Decimal
    fee: Decimal
    net: Decimal
    original_ref: str | None
    original_on: date | None


@dataclass(frozen=True, slots=True)
class Contract:
    source: SourceRef
    merchant: str
    cnpj: str
    establishment_code: str | None
    rates: dict[str, Decimal]
    remittance_rate: Decimal | None
    validity: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class RemittanceEntry:
    source: SourceRef
    contracted_on: date
    settled_on: date | None
    cnpj: str
    merchant: str
    currency: str
    foreign_amount: Decimal
    customer_rate: Decimal
    ptax_rate: Decimal
    brl_amount: Decimal
    spread: Decimal
    spread_rate: Decimal
    estimated_spread: Decimal
    iof: Decimal
    status: str
    note: str | None


@dataclass(frozen=True, slots=True)
class BankEntry:
    source: SourceRef
    bank: Literal["AURORA", "MERIDIANO"]
    occurred_on: date
    description: str
    amount: Decimal
    balance: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Account:
    source: SourceRef
    code: str
    description: str
    kind: str
    nature: str
    posting_allowed: bool


@dataclass(frozen=True, slots=True)
class Dataset:
    checkout: tuple[CheckoutEntry, ...]
    contracts: tuple[Contract, ...]
    remittances: tuple[RemittanceEntry, ...]
    banks: tuple[BankEntry, ...]
    accounts: tuple[Account, ...]
    bank_opening_balances: dict[str, Decimal]
    bank_closing_balances: dict[str, Decimal]
