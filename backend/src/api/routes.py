from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from src.application.closing import close_month
from src.application.diagnostics import DiagnosticReport, diagnose
from src.domain.closing import ClosingResult
from src.domain.models import Dataset
from src.infrastructure.exports import CSV_FILES, render_csv
from src.infrastructure.readers import load_dataset


def parse_period(
    competencia: Annotated[
        str,
        Query(
            pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$",
            description="Competência no formato AAAA-MM",
            examples=["2026-09"],
        ),
    ],
) -> date:
    try:
        return date.fromisoformat(f"{competencia}-01")
    except ValueError as error:
        raise HTTPException(422, "Competência inválida: use ano entre 0001 e 9999.") from error


def create_router(data_dir: Path | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def configured_dataset() -> Dataset:
        if data_dir is None:
            raise HTTPException(503, "Diretório de dados não configurado. Defina TARVO_DATA_DIR.")
        return load_dataset(data_dir)

    @router.get("/diagnostico", response_model=DiagnosticReport)
    def diagnostico(
        period: Annotated[date, Depends(parse_period)],
        dataset: Annotated[Dataset, Depends(configured_dataset)],
    ) -> DiagnosticReport:
        return diagnose(dataset, period)

    @router.get("/fechamento", response_model=ClosingResult)
    def fechamento(
        period: Annotated[date, Depends(parse_period)],
        dataset: Annotated[Dataset, Depends(configured_dataset)],
    ) -> ClosingResult:
        return close_month(dataset, period)

    @router.get("/fechamento/{arquivo}", response_class=Response)
    def arquivo_fechamento(
        arquivo: str,
        period: Annotated[date, Depends(parse_period)],
        dataset: Annotated[Dataset, Depends(configured_dataset)],
    ) -> Response:
        if arquivo not in CSV_FILES:
            raise HTTPException(404, "Arquivo de fechamento desconhecido.")
        content = render_csv(close_month(dataset, period), arquivo)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{arquivo}"'},
        )

    return router
