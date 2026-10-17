import csv
from dataclasses import fields
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from src.domain.closing import (
    ClassifiedMovement,
    ClosingResult,
    Commission,
    Divergence,
    Invoice,
    Pending,
    Trace,
)

CSV_FILES = {
    "comissoes_por_lojista.csv": ("comissoes_por_lojista", Commission),
    "divergencias_taxa.csv": ("divergencias_taxa", Divergence),
    "notas_a_emitir.csv": ("notas_a_emitir", Invoice),
    "lancamentos_classificados.csv": ("lancamentos_classificados", ClassifiedMovement),
    "pendencias.csv": ("pendencias", Pending),
    "rastro.csv": ("rastro", Trace),
}

def _value(value: object) -> str:
    if value is None:
        return ""
    return format(value, "f") if isinstance(value, Decimal) else str(value)



def render_csv(result: ClosingResult, filename: str) -> str:
    attribute, model = CSV_FILES[filename]
    columns = tuple(field.name for field in fields(model))
    stream = StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerow(columns)
    for row in getattr(result, attribute):
        writer.writerow(_value(getattr(row, column)) for column in columns)
    return stream.getvalue()


def export_csvs(result: ClosingResult, output_dir: Path) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tarvo-", dir=output_dir.parent) as temporary:
        staging = Path(temporary)
        for filename in CSV_FILES:
            (staging / filename).write_text(render_csv(result, filename), encoding="utf-8", newline="")
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename in CSV_FILES:
            (staging / filename).replace(output_dir / filename)
