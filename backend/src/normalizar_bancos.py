from argparse import ArgumentParser
from pathlib import Path
import sys

from src.application.banking_reference import _period, approve_bank_reference, generate_bank_reference
from src.domain.models import DataPackError
from src.infrastructure.normalization_sources import json_text
from src.infrastructure.readers import load_dataset


def main() -> None:
    parser = ArgumentParser(description="Sugestões bancárias com RCA e revisão explícita antes do fechamento.")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("gerar", help="Gerar propostas não aplicadas usando OpenAI.")
    approve = commands.add_parser("aprovar", help="Aprovar movimentos selecionados sem consultar OpenAI.")
    for command in (generate, approve):
        command.add_argument("--entrada", type=Path, required=True)
        command.add_argument("--docs", type=Path, default=Path("docs"))
        command.add_argument("--saida", type=Path, required=True)
    generate.add_argument("--competencia", required=True)
    generate.add_argument("--env-file", type=Path, default=Path(".env"))
    approve.add_argument("--referencias", type=Path, required=True)
    approve.add_argument("--revisor", required=True)
    approve.add_argument("--movimento", action="append")
    approve.add_argument("--todos-validos", action="store_true")
    args = parser.parse_args()
    try:
        period = _period(args.competencia) if args.command == "gerar" else None
        dataset = load_dataset(args.entrada)
        if args.command == "gerar":
            from src.infrastructure.openai_normalizer import ModelUnavailable, OpenAIInterpreter

            try:
                interpreter = OpenAIInterpreter(args.env_file, args.saida / "cache_llm")
                report = generate_bank_reference(dataset, args.entrada, args.docs, args.saida, period, interpreter)
            except ModelUnavailable as error:
                parser.exit(2, f"Geração bancária não concluída: {error}\n")
        else:
            import json
            selected = args.movimento or []
            if args.todos_validos:
                raw = json.loads(args.referencias.read_text(encoding="utf-8"))
                selected = [item["movement_id"] for item in raw.get("cases", []) if item.get("proposal")]
            report = approve_bank_reference(dataset, args.entrada, args.docs, args.referencias,
                args.saida, args.revisor, selected)
    except (DataPackError, ValueError, OSError) as error:
        parser.exit(2, f"Referência bancária não concluída: {error}\n")
    sys.stdout.write(json_text(report["summary"]) + "\n")


if __name__ == "__main__":
    main()
