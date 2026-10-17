from argparse import ArgumentParser
from pathlib import Path
import sys

from src.application.normalization import normalize_reference, validate_output_dir
from src.application.cache_log import convert_cache_log
from src.domain.models import DataPackError
from src.infrastructure.flow_log import FlowLog
from src.infrastructure.normalization_sources import json_text
from src.infrastructure.openai_normalizer import ModelUnavailable, OpenAIInterpreter
from src.infrastructure.readers import load_dataset
from src.infrastructure.slack_inbox import find_inbox_file, parse_inbox


def main() -> None:
    parser = ArgumentParser(description="Probabilístico → join → RCA → escalada humana, com OpenAI.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--entrada", type=Path)
    source.add_argument("--converter-cache", type=Path)
    parser.add_argument("--saida", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--caixa-de-entrada", type=Path, help="Arquivo da caixa de entrada do Slack (JSON, CSV ou texto).")
    parser.add_argument("--limite-amostras", type=int, default=5)
    parser.add_argument("--limite-candidatos", type=int, default=8)
    parser.add_argument("--score-fuzzy", type=float, default=75)
    args = parser.parse_args()
    try:
        if args.converter_cache:
            report = convert_cache_log(args.converter_cache, args.saida)
            sys.stdout.write(json_text(report) + "\n")
            return
        validate_output_dir(args.entrada, args.saida)
        with FlowLog(args.saida) as log:
            interpreter = OpenAIInterpreter(args.env_file, args.saida / "cache_llm", log=log)
            log.event("CONFIGURACAO", "MODELO_CONFIGURADO", model=interpreter.model)
            log.event("INGESTAO", "LEITURA_PACOTE_INICIADA")
            dataset = load_dataset(args.entrada)
            log.event("INGESTAO", "LEITURA_PACOTE_CONCLUIDA", checkout_rows=len(dataset.checkout),
                contract_rows=len(dataset.contracts))
            inbox_file = find_inbox_file(explicit_path=args.caixa_de_entrada) if args.caixa_de_entrada else None
            inbox = parse_inbox(inbox_file) if inbox_file else None
            if inbox:
                log.event("CONFIGURACAO", "CAIXA_DE_ENTRADA_CARREGADA",
                    arquivo=inbox.source_file, vinculos=len(inbox.bindings),
                    isencoes=len(inbox.exemptions), bloqueios=len(inbox.blocks))
            report = normalize_reference(dataset, args.entrada, args.saida,
                interpreter, samples=args.limite_amostras, threshold=args.score_fuzzy,
                candidate_limit=args.limite_candidatos, log=log, inbox=inbox)
    except (DataPackError, ModelUnavailable, ValueError, OSError) as error:
        parser.exit(2, f"Normalização não concluída: {error}\n")
    sys.stdout.write(json_text(report["summary"]) + "\n")


if __name__ == "__main__":
    main()
