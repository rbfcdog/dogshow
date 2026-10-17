from dataclasses import asdict
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.api.routes import create_router
from src.domain.models import DataPackError


def create_app(data_dir: Path | None = None) -> FastAPI:
    if data_dir is None:
        configured_dir = os.environ.get("TARVO_DATA_DIR")
        data_dir = Path(configured_dir) if configured_dir else None

    application = FastAPI(
        title="Tarvo — fechamento financeiro",
        description="Comissões contratadas, remessas, notas propostas, classificação bancária e rastro.",
        version="0.2.0",
    )

    @application.exception_handler(DataPackError)
    def invalid_data_pack(_request: Request, error: DataPackError) -> JSONResponse:
        detail = {"message": str(error)}
        if error.source is not None:
            source = asdict(error.source)
            source["file"] = Path(error.source.file).name
            detail["source"] = source
        return JSONResponse(status_code=422, content={"detail": detail})

    @application.get("/health", summary="Verificar se a API está em execução")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    application.include_router(create_router(data_dir))
    return application


app = create_app()
