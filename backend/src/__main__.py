from argparse import ArgumentParser
from datetime import date
from pathlib import Path
import re
import sys

from src.application.closing import close_month
from src.domain.models import DataPackError
from src.infrastructure.exports import export_csvs
from src.infrastructure.readers import load_dataset
from src.infrastructure.openai_normalizer import ModelUnavailable, OpenAIInterpreter
from src.infrastructure.summary import render_summary

CONTRACT_REFERENCE = Path(".tarvo-work") / "referencias" / "referencias.json"
BANK_PROPOSALS = Path(".tarvo-work") / "referencias_bancarias_novas" / "referencias_bancarias.json"


def resolve_env_file(path: Path) -> Path:
    if path.is_file() or path != Path(".env"):
        return path
    return Path(__file__).resolve().parents[2] / ".env"


def main() -> None:
    parser = ArgumentParser(description="Fechamento Tarvo: lê os arquivos do mês, normaliza contratos com IA, "
        "gera os seis CSVs e imprime o resumo.")
    parser.add_argument("--entrada", required=True, type=Path)
    parser.add_argument("--competencia", required=True)
    parser.add_argument("--saida", required=True, type=Path)
    parser.add_argument("--referencias", type=Path, help="Destino da referência contratual gerada por IA; padrão: .tarvo-work/referencias/referencias.json.")
    parser.add_argument("--referencias-bancarias", type=Path, help="Referência bancária revisada; prevalece sobre propostas.")
    parser.add_argument("--propostas-bancarias", type=Path, default=BANK_PROPOSALS,
        help="Propostas da IA validadas contra as fontes atuais; sem aprovação, geram pendências. Padrão: .tarvo-work/referencias_bancarias_novas/referencias_bancarias.json.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--docs", type=Path, default=Path("docs"))
    parser.add_argument("--caixa-de-entrada", type=Path,
        help="Arquivo da caixa de entrada do Slack (JSON, CSV ou texto).")
    parser.add_argument("--sem-politicas", action="store_true",
        help="Ignora o arquivo de políticas confirmadas e usa só os dados brutos.")
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", arguments.competencia):
        parser.error("Competência inválida: use AAAA-MM.")
    try:
        period = date.fromisoformat(f"{arguments.competencia}-01")
    except ValueError:
        parser.error("Competência inválida: use ano entre 0001 e 9999.")
    applied: list[str] = []
    warnings: list[str] = []
    contract_file = arguments.referencias or CONTRACT_REFERENCE
    bank_file = arguments.referencias_bancarias
    if contract_file.name != "referencias.json":
        parser.error("A normalização grava referencias.json; use --referencias apontando para esse nome.")
    try:
        dataset = load_dataset(arguments.entrada)
        policies = None
        if not arguments.sem_politicas:
            from src.infrastructure.slack_inbox import find_inbox_file, parse_inbox, default_inbox
            inbox_file = find_inbox_file(arguments.entrada, arguments.caixa_de_entrada)
            policies = parse_inbox(inbox_file) if inbox_file else default_inbox()
            applied.append(f"políticas confirmadas ({policies.source_file}): {len(policies.bindings)} vínculos, {len(policies.exemptions)} isenções")
        bank_decisions = None
        bank_proposals = None
        if bank_file is not None:
            from src.application.banking_reference import load_bank_reference
            bank_decisions = load_bank_reference(dataset, arguments.entrada, arguments.docs, bank_file, period)
            applied.append(f"bancária aprovada por revisor ({len(bank_decisions)} decisões): {bank_file}")
        else:
            from src.application.banking_reference import load_bank_proposals
            bank_proposals = load_bank_proposals(dataset, arguments.entrada, arguments.docs,
                arguments.propostas_bancarias, period)
            applied.append(f"propostas bancárias da IA ({len(bank_proposals)} contas não aprovadas): {arguments.propostas_bancarias}")
            warnings.append("Contas propostas por IA exigem validação humana antes de lançamento no ERP.")
        from src.application.normalization import normalize_reference, load_reference
        from src.infrastructure.flow_log import FlowLog

        with FlowLog(contract_file.parent) as log:
            interpreter = OpenAIInterpreter(resolve_env_file(arguments.env_file),
                contract_file.parent / "cache_llm", log=log, reuse_cache=False)
            normalize_reference(dataset, arguments.entrada, contract_file.parent, interpreter, log=log, inbox=policies)
        applied.append(f"normalização com IA ({interpreter.model})")
        dataset = load_reference(dataset, arguments.entrada, contract_file, inbox=policies)
        applied.append(f"contratual: {contract_file}")
        result = close_month(dataset, period, bank_decisions=bank_decisions,
            bank_proposals=bank_proposals, policies=policies)
        late_count = sum(1 for e in dataset.checkout if e.source.file.startswith("06_orbe"))
        export_csvs(result, arguments.saida)
    except DataPackError as error:
        parser.exit(2, f"Pacote inválido: {error}\n")
    except (ValueError, OSError, ModelUnavailable) as error:
        parser.exit(2, f"Fechamento não concluído: {error}\n")
    sys.stdout.write(render_summary(result, tuple(applied), tuple(warnings), str(arguments.saida), late_adjustments=late_count))


if __name__ == "__main__":
    main()
