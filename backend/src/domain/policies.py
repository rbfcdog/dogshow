from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from src.domain.models import Contract, DataPackError

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def period_key(period: date) -> str:
    return f"{period.year:04d}-{period.month:02d}"


def in_period(value: date, period: date) -> bool:
    return (value.year, value.month) == (period.year, period.month)


def output_id(kind: str, period: date, *parts: str) -> str:
    return ":".join((kind, period_key(period), *parts))


def active_on(contract: Contract, on_date: date) -> bool:
    if "INATIVO" in (contract.note or "").upper():
        return False
    if contract.structured_validity:
        return (
            (contract.validity_start is not None or contract.validity_end is not None)
            and (contract.validity_start is None or on_date >= contract.validity_start)
            and (contract.validity_end is None or on_date <= contract.validity_end)
        )
    validity = contract.validity
    if not validity:
        return False
    try:
        if validity.startswith("até "):
            return on_date <= datetime.strptime(validity[4:], "%d/%m/%Y").date()
        if validity.startswith("a partir de "):
            return on_date >= datetime.strptime(validity[12:], "%d/%m/%Y").date()
        return on_date >= datetime.strptime(validity, "%d/%m/%Y").date()
    except ValueError as error:
        raise DataPackError("Vigência contratual não reconhecida.", contract.source) from error


def contract_candidates(
    contracts: tuple[Contract, ...], on_date: date, *,
    establishment_code: str | None = None, cnpj: str | None = None,
) -> tuple[Contract, ...]:
    if establishment_code is not None:
        candidates = (
            contract for contract in contracts
            if contract.establishment_code == establishment_code
        )
    elif cnpj is not None:
        candidates = (contract for contract in contracts if contract.cnpj == cnpj)
    else:
        raise ValueError("Informe EC ou CNPJ para identificar o contrato.")
    return tuple(contract for contract in candidates if active_on(contract, on_date))


def has_commercial_question(contract: Contract) -> bool:
    evidence = ((contract.note or "") + " " + (contract.comments or "")).casefold()
    return any(term in evidence for term in ("comercial", "condição especial", "condicao especial"))
