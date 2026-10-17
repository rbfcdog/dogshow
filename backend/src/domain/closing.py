from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from src.domain.models import SourceRef

BusinessLine = Literal["CHECKOUT", "REMESSA"]


@dataclass(frozen=True, slots=True)
class Commission:
    id_saida: str
    cnpj: str
    lojista: str
    linha_negocio: BusinessLine
    comissao_brl: Decimal


@dataclass(frozen=True, slots=True)
class Divergence:
    id_saida: str
    cnpj: str
    linha_negocio: BusinessLine
    ref_origem: str
    taxa_contratada_pct: Decimal
    taxa_aplicada_pct: Decimal
    diferenca_brl: Decimal


@dataclass(frozen=True, slots=True)
class Invoice:
    id_saida: str
    tomador_cnpj: str
    tomador: str
    linha_negocio: BusinessLine
    valor_brl: Decimal
    discriminacao: str


@dataclass(frozen=True, slots=True)
class ClassifiedMovement:
    id_saida: str
    banco: Literal["AURORA", "MERIDIANO"]
    ref_origem: str
    data: str
    descricao: str
    valor: Decimal
    conta_codigo: str
    metodo: Literal["REGRA", "IA", "HUMANO"]
    confianca: Decimal


@dataclass(frozen=True, slots=True)
class Pending:
    id_saida: str
    tipo: str
    ref_origem: str
    valor_brl: Decimal | None
    motivo: str
    quem_decide: str


@dataclass(frozen=True, slots=True)
class Trace:
    id_saida: str
    arquivo_origem: str
    ref_origem: str
    valor: Decimal
    linha_origem: int | None = None
    aba_origem: str | None = None
    regra: str = ""


@dataclass(frozen=True, slots=True)
class ClosingPart:
    commissions: tuple[Commission, ...] = ()
    divergences: tuple[Divergence, ...] = ()
    invoices: tuple[Invoice, ...] = ()
    movements: tuple[ClassifiedMovement, ...] = ()
    pending: tuple[Pending, ...] = ()
    traces: tuple[Trace, ...] = ()


@dataclass(frozen=True, slots=True)
class ClosingResult:
    competencia: str
    status: Literal["CONCLUIDO", "CONCLUIDO_COM_PENDENCIAS", "PRELIMINAR_COM_PENDENCIAS"]
    comissoes_por_lojista: tuple[Commission, ...]
    divergencias_taxa: tuple[Divergence, ...]
    notas_a_emitir: tuple[Invoice, ...]
    lancamentos_classificados: tuple[ClassifiedMovement, ...]
    pendencias: tuple[Pending, ...]
    rastro: tuple[Trace, ...]
    premissas_tecnicas: tuple[str, ...] = ()


def trace(identifier: str, source: SourceRef, amount: Decimal, rule: str) -> Trace:
    return Trace(identifier, source.file, source.ref, amount, source.row, source.sheet, rule)
