from argparse import ArgumentParser
from datetime import date
from pathlib import Path
import re

from src.application.closing import close_month
from src.domain.models import DataPackError
from src.infrastructure.exports import export_csvs
from src.infrastructure.readers import load_dataset


def main() -> None:
    parser = ArgumentParser(description="Fechamento Tarvo com regras confirmadas e rastro por origem.")
    parser.add_argument("--entrada", required=True, type=Path)
    parser.add_argument("--competencia", required=True)
    parser.add_argument("--saida", required=True, type=Path)
    parser.add_argument("--referencias", type=Path)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", arguments.competencia):
        parser.error("Competência inválida: use AAAA-MM.")
    try:
        period = date.fromisoformat(f"{arguments.competencia}-01")
    except ValueError:
        parser.error("Competência inválida: use ano entre 0001 e 9999.")
    try:
        dataset = load_dataset(arguments.entrada)
        if arguments.referencias:
            from src.application.normalization import load_reference

            dataset = load_reference(dataset, arguments.entrada, arguments.referencias)
        result = close_month(dataset, period)
        export_csvs(result, arguments.saida)
    except DataPackError as error:
        parser.exit(2, f"Pacote inválido: {error}\n")
    except (ValueError, OSError) as error:
        parser.exit(2, f"Fechamento não concluído: {error}\n")


if __name__ == "__main__":
    main()
