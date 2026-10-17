from argparse import ArgumentParser
from pathlib import Path
import sys

from src.application.normalization import normalize_reference
from src.domain.models import DataPackError
from src.infrastructure.normalization_sources import json_text
from src.infrastructure.openai_normalizer import ModelUnavailable, OpenAIInterpreter
from src.infrastructure.readers import load_dataset


def main() -> None:
    parser = ArgumentParser(description="Probabilístico → join → RCA → escalada humana, com OpenAI.")
    parser.add_argument("--entrada", type=Path, required=True)
    parser.add_argument("--docs", type=Path, default=Path("docs"))
    parser.add_argument("--saida", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--limite-amostras", type=int, default=5)
    parser.add_argument("--limite-candidatos", type=int, default=8)
    parser.add_argument("--score-fuzzy", type=float, default=75)
    args = parser.parse_args()
    try:
        interpreter = OpenAIInterpreter(args.env_file, args.saida / "cache_llm")
        report = normalize_reference(load_dataset(args.entrada), args.entrada, args.docs, args.saida,
            interpreter, samples=args.limite_amostras, threshold=args.score_fuzzy,
            candidate_limit=args.limite_candidatos)
    except (DataPackError, ModelUnavailable, ValueError, OSError) as error:
        parser.exit(2, f"Normalização não concluída: {error}\n")
    sys.stdout.write(json_text(report["summary"]) + "\n")


if __name__ == "__main__":
    main()
