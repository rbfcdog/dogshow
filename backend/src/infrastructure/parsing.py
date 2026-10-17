import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from src.domain.models import DataPackError, SourceRef


def text(value: object, source: SourceRef, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataPackError(f"{field}: texto obrigatório inválido.", source)
    return value.strip()


def optional_text(value: object, source: SourceRef, field: str) -> str | None:
    return None if blank(value) else text(value, source, field)


def blank(value: object) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def decimal_value(
    value: object, source: SourceRef, field: str, *, ptbr: bool = False
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise DataPackError(f"{field}: número obrigatório inválido.", source)
    raw = str(value).strip()
    if ptbr and isinstance(value, str):
        if not re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+)(?:,[0-9]+)?", raw):
            raise DataPackError(f"{field}: número pt-BR inválido.", source)
        raw = raw.replace(".", "").replace(",", ".")
    elif isinstance(value, str) and not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw):
        raise DataPackError(f"{field}: número inválido.", source)
    try:
        result = Decimal(raw)
    except InvalidOperation:
        raise DataPackError(f"{field}: número inválido.", source) from None
    if not result.is_finite():
        raise DataPackError(f"{field}: número deve ser finito.", source)
    return result


def integer(value: object, source: SourceRef, field: str) -> int:
    result = decimal_value(value, source, field)
    if result != result.to_integral_value():
        raise DataPackError(f"{field}: inteiro inválido.", source)
    return int(result)


def identifier(value: object, source: SourceRef, field: str) -> str:
    if isinstance(value, str):
        result = value.strip()
        if re.fullmatch(r"[0-9]+", result):
            return result
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        result = str(integer(value, source, field))
        if result.isascii() and result.isdigit():
            return result
    raise DataPackError(f"{field}: identificador numérico inválido.", source)


def cnpj(value: object, source: SourceRef) -> str:
    if isinstance(value, str):
        raw = value.strip()
        if not re.fullmatch(r"[0-9]+|[0-9]{2}\.[0-9]{3}\.[0-9]{3}/[0-9]{4}-[0-9]{2}", raw):
            raise DataPackError("CNPJ: formato inválido.", source)
        digits = re.sub(r"[./-]", "", raw)
    else:
        digits = identifier(value, source, "CNPJ")
    if len(digits) > 14:
        raise DataPackError("CNPJ: deve conter no máximo 14 dígitos.", source)
    return digits.zfill(14)


def percentage(
    value: object,
    source: SourceRef,
    field: str,
    *,
    percentage_points: bool = False,
) -> Decimal:
    explicit_percent = isinstance(value, str) and value.strip().endswith("%")
    if explicit_percent:
        value = value.strip()[:-1].strip()
    result = decimal_value(
        value,
        source,
        field,
        ptbr=isinstance(value, str) and (percentage_points or "," in value),
    )
    in_points = percentage_points or explicit_percent
    if result.copy_abs() > (100 if in_points else 1):
        raise DataPackError(f"{field}: percentual fora do intervalo de -100% a 100%.", source)
    return result / Decimal(100) if in_points else result


def day(value: object, source: SourceRef, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", value.strip()):
        try:
            return datetime.strptime(value.strip(), "%d/%m/%Y").date()
        except ValueError:
            pass
    raise DataPackError(f"{field}: data inválida.", source)


def ofx_day(value: str, source: SourceRef, field: str) -> date:
    match = re.fullmatch(r"([0-9]{8})([0-9]{6})?(?:\.[0-9]+)?(?:\[[^\]]+\])?", value)
    if match:
        raw = match[1] + (match[2] or "")
        try:
            return datetime.strptime(raw, "%Y%m%d%H%M%S" if match[2] else "%Y%m%d").date()
        except ValueError:
            pass
    raise DataPackError(f"{field}: data OFX inválida.", source)
