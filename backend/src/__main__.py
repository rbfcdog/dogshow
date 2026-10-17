from argparse import ArgumentParser
from datetime import date
from pathlib import Path
import re
import sys

from src.application.closing import close_month
from src.domain.models import DataPackError
from src.infrastructure.exports import export_csvs
from src.infrastructure.readers import load_dataset
from src.infrastructure.summary import render_summary

CONTRACT_REFERENCE = Path("referencias") / "referencias.json"
BANK_REFERENCE = Path("referencias_bancarias") / "revisada-v0.json"


def main() -> None:
    parser = ArgumentParser(description="Fechamento Tarvo: um comando lê os arquivos do mês, aplica as referências "
        "revisadas, gera os seis CSVs e imprime o resumo.")
    parser.add_argument("--entrada", required=True, type=Path)
    parser.add_argument("--competencia", required=True)
    parser.add_argument("--saida", required=True, type=Path)
    parser.add_argument("--referencias", type=Path, help="Referência contratual; padrão: <saida>/referencias/referencias.json se existir.")
    parser.add_argument("--referencias-bancarias", type=Path,
        help="Referência bancária aprovada; padrão: <saida>/referencias_bancarias/revisada-v0.json se existir.")
    parser.add_argument("--sem-referencias", action="store_true", help="Ignora referências e usa só os dados originais.")
    parser.add_argument("--regerar", action="store_true",
        help="Chama a LLM antes de fechar para gerar a referência contratual (exige OPENAI_API_KEY e OPENAI_MODEL no .env).")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--docs", type=Path, default=Path("docs"))
    parser.add_argument("--caixa-de-entrada", type=Path,
        help="Arquivo da caixa de entrada do Slack (JSON, CSV ou texto).")
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", arguments.competencia):
        parser.error("Competência inválida: use AAAA-MM.")
    try:
        period = date.fromisoformat(f"{arguments.competencia}-01")
    except ValueError:
        parser.error("Competência inválida: use ano entre 0001 e 9999.")
    if arguments.sem_referencias and (arguments.regerar or arguments.referencias or arguments.referencias_bancarias):
        parser.error("--sem-referencias não combina com --regerar, --referencias ou --referencias-bancarias.")
    applied: list[str] = []
    warnings: list[str] = []
    contract_file = arguments.referencias or arguments.saida / CONTRACT_REFERENCE
    bank_file = arguments.referencias_bancarias or arguments.saida / BANK_REFERENCE
    if arguments.regerar and contract_file.name != "referencias.json":
        parser.error("--regerar grava referencias.json; use --referencias apontando para um arquivo com esse nome.")
    try:
        dataset = load_dataset(arguments.entrada)
        from src.infrastructure.slack_inbox import find_inbox_file, parse_inbox
        inbox_file = find_inbox_file(arguments.entrada, arguments.caixa_de_entrada)
        inbox = parse_inbox(inbox_file) if inbox_file else None
        if inbox:
            applied.append(f"caixa de entrada do Slack ({inbox.source_file}): {len(inbox.bindings)} vínculos, {len(inbox.exemptions)} isenções")
        if arguments.regerar:
            from src.application.normalization import normalize_reference
            from src.infrastructure.flow_log import FlowLog
            from src.infrastructure.openai_normalizer import OpenAIInterpreter

            with FlowLog(contract_file.parent) as log:
                interpreter = OpenAIInterpreter(arguments.env_file, contract_file.parent / "cache_llm", log=log)
                normalize_reference(dataset, arguments.entrada, contract_file.parent, interpreter, log=log, inbox=inbox)
        bank_decisions = None
        if not arguments.sem_referencias:
            if bank_file.is_file():
                from src.application.banking_reference import load_bank_reference

                try:
                    bank_decisions = load_bank_reference(
                        dataset=dataset, data_dir=arguments.entrada, docs_dir=arguments.docs,
                        reference_file=bank_file, period=period,
                    )
                    applied.append(f"bancária aprovada por revisor ({len(bank_decisions)} decisões): {bank_file}")
                except ValueError as error:
                    if arguments.referencias_bancarias:
                        raise
                    warnings.append(f"referência bancária {bank_file} NÃO aplicada ({error})")
            elif arguments.referencias_bancarias:
                raise ValueError(f"Referência bancária informada não existe: {bank_file}")
            if contract_file.is_file():
                from src.application.normalization import load_reference

                try:
                    dataset = load_reference(dataset, arguments.entrada, contract_file, inbox=inbox)
                    applied.append(f"contratual: {contract_file}")
                except ValueError as error:
                    if arguments.referencias:
                        raise
                    warnings.append(f"referência contratual {contract_file} NÃO aplicada ({error})")
            elif arguments.referencias:
                raise ValueError(f"Referência contratual informada não existe: {contract_file}")
        result = close_month(dataset, period, bank_decisions=bank_decisions)
        export_csvs(result, arguments.saida)
    except DataPackError as error:
        parser.exit(2, f"Pacote inválido: {error}\n")
    except (ValueError, OSError) as error:
        parser.exit(2, f"Fechamento não concluído: {error}\n")
    sys.stdout.write(render_summary(result, tuple(applied), tuple(warnings), str(arguments.saida)))


if __name__ == "__main__":
    main()
