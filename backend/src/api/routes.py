from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from src.application.closing import close_month
from src.application.diagnostics import DiagnosticReport, diagnose
from src.domain.closing import ClosingResult
from src.domain.models import Dataset
from src.infrastructure.exports import CSV_FILES, export_csvs, render_csv
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

    @router.post("/fechamento/executar")
    def executar_fechamento(
        period: Annotated[date, Depends(parse_period)],
        dataset: Annotated[Dataset, Depends(configured_dataset)],
    ):
        from src.infrastructure.openai_normalizer import ModelUnavailable, OpenAIInterpreter
        from src.application.normalization import load_reference, normalize_reference
        from src.infrastructure.flow_log import FlowLog
        from src.infrastructure.slack_inbox import default_inbox, find_inbox_file, parse_inbox
        from src.application.banking_reference import generate_bank_reference, load_bank_proposals

        assert data_dir is not None
        saida_dir = Path("saida")
        contract_dir = Path(".tarvo-work/referencias")
        bank_file = Path(".tarvo-work/referencias_bancarias_novas/referencias_bancarias.json")
        warnings = ["Contas propostas por IA exigem validação humana antes de lançamento no ERP."]
        inbox_file = find_inbox_file(data_dir)
        policies = parse_inbox(inbox_file) if inbox_file else default_inbox()
        try:
            if not bank_file.exists():
                bank_interpreter = OpenAIInterpreter(Path(".env"), bank_file.parent / "cache_llm",
                    reuse_cache=False, env_file_only=True)
                generate_bank_reference(dataset, data_dir, Path("docs"), bank_file.parent, period, bank_interpreter)
            bank_proposals = load_bank_proposals(dataset, data_dir, Path("docs"), bank_file, period)
        except ModelUnavailable as error:
            raise HTTPException(502, str(error)) from error
        except (ValueError, OSError) as error:
            raise HTTPException(409, f"Propostas bancárias inválidas ou obsoletas: {error}") from error
        try:
            with FlowLog(contract_dir) as log:
                interpreter = OpenAIInterpreter(Path(".env"), contract_dir / "cache_llm", log=log,
                    reuse_cache=False, env_file_only=True)
                report = normalize_reference(dataset, data_dir, contract_dir, interpreter, log=log, inbox=policies)
        except ModelUnavailable as error:
            raise HTTPException(502, str(error)) from error
        dataset = load_reference(dataset, data_dir, contract_dir / "referencias.json", inbox=policies)
        applied = [f"normalização com IA ({interpreter.model})", f"contratual: {contract_dir / 'referencias.json'}",
            f"propostas bancárias da IA ({len(bank_proposals)} contas não aprovadas): {bank_file}"]
        result = close_month(dataset, period, bank_proposals=bank_proposals, policies=policies)
        late_count = sum(entry.source.file.startswith("06_orbe") for entry in dataset.checkout)
        export_csvs(result, saida_dir)
        from src.infrastructure.summary import render_summary
        summary_text = render_summary(result, tuple(applied), tuple(warnings), str(saida_dir), late_adjustments=late_count)
        return {
            "success": True,
            "competencia": result.competencia,
            "status": result.status,
            "summary_text": summary_text,
            "comissoes_total": sum(c.comissao_brl for c in result.comissoes_por_lojista),
            "notas_total": sum(n.valor_brl for n in result.notas_a_emitir),
            "divergencias_total": sum(d.diferenca_brl for d in result.divergencias_taxa),
            "pendencias_count": len(result.pendencias),
            "rastro_count": len(result.rastro),
            "applied_references": applied,
            "normalization": report["summary"],
            "model": interpreter.model,
            "warnings": warnings,
        }

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
