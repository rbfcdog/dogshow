from collections import Counter
import csv
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from src.application.normalization import normalize_reference
from src.infrastructure.readers import load_dataset
from src.infrastructure.slack_inbox import (
    SlackInbox,
    find_inbox_file,
    parse_inbox,
)
from tests.check_normalization import _ReferenceInterpreter

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "dados_tarvo"
THREADS_FILE = ROOT / "docs" / "THREADS_FINANCEIRO_TARVO.md"


def check_parsing_markdown() -> None:
    inbox = parse_inbox(THREADS_FILE)
    assert inbox.source_file == THREADS_FILE.name
    assert "1038867654" in inbox.bindings
    assert inbox.bindings["1038867654"].merchant == "Kiwi Experience"
    assert "1043396662" in inbox.bindings
    assert "1043300981" in inbox.bindings
    assert "00520815000100" in inbox.exemptions
    exemption = inbox.exemptions["00520815000100"]
    assert exemption.rate == Decimal("0.00")
    assert exemption.start_date == date(2026, 9, 1)
    assert exemption.end_date == date(2026, 12, 31)
    assert exemption.preserve_historical_sales is True
    assert "1076801732" in inbox.blocks
    assert inbox.blocks["1076801732"].faturar is False
    assert inbox.spreads.get("17217888000111").spread == Decimal("0.012")
    assert inbox.spreads.get(None).spread == Decimal("0.015")
    assert len(inbox.chunks) > 100


def check_parsing_structured_json() -> None:
    payload = {
        "bindings": [
            {"ec": "1038867654", "cnpj": "07752116000126", "merchant": "Kiwi Experience"},
            {"ec": "1043396662", "cnpj": "78576234000179", "merchant": "Horizonte Viagens"},
        ],
        "exemptions": [
            {
                "cnpj": "00520815000100",
                "merchant": "Casa do Estudante",
                "rate": "0.00",
                "start_date": "2026-09-01",
                "end_date": "2026-12-31",
                "preserve_historical_sales": True,
            }
        ],
        "blocks": [
            {"ec": "1076801732", "merchant": "Atlas Idiomas", "faturar": False, "reason": "Sem contrato vigente"}
        ],
        "spreads": {
            "default": {"spread": "0.015"},
            "17217888000111": {"spread": "0.012"},
        },
    }
    with TemporaryDirectory() as directory:
        file_path = Path(directory) / "caixa_de_entrada.json"
        file_path.write_text(json.dumps(payload), encoding="utf-8")
        inbox = parse_inbox(file_path)
        assert len(inbox.bindings) == 2
        assert "1038867654" in inbox.bindings
        assert "00520815000100" in inbox.exemptions
        assert inbox.blocks["1076801732"].reason == "Sem contrato vigente"
        assert inbox.spreads[None].spread == Decimal("0.015")


def check_normalized_table_and_join() -> None:
    dataset = load_dataset(DATA_DIR)
    inbox = parse_inbox(THREADS_FILE)
    with TemporaryDirectory() as directory:
        output_dir = Path(directory) / "normalizacao"
        report = normalize_reference(
            dataset, DATA_DIR, output_dir, _ReferenceInterpreter(), samples=2, inbox=inbox
        )
        assert report["summary"]["joined"] == 1900
        assert report["summary"]["unresolved_rows"] == 57

        with (output_dir / "contratos_normalizados.csv").open(encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        assert len(rows) > 100
        casa_rows = [r for r in rows if r["cnpj"] == "00520815000100"]
        assert len(casa_rows) == 14  # 7 modalidades históricas + 7 modalidades de set–dez
        exempt_rows = [r for r in casa_rows if r["status"] == "ISENTO_INBOX"]
        assert len(exempt_rows) == 7
        assert all(r["taxa_fracao"] == "0.00" for r in exempt_rows if r["modalidade"] != "REMESSA")
        assert all(r["validity_start"] == "2026-09-01" for r in exempt_rows)

        hist_rows = [r for r in casa_rows if r["validity_end"] == "2026-08-31"]
        assert len(hist_rows) == 7
        debito_hist = next(r for r in hist_rows if r["modalidade"] == "DEBITO")
        assert debito_hist["taxa_fracao"] == "0.0149"

        with (output_dir / "join_checkout.csv").open(encoding="utf-8") as stream:
            join_rows = list(csv.DictReader(stream, delimiter=";"))
        assert len(join_rows) == 1957
        status_counts = Counter(r["status"] for r in join_rows)
        assert status_counts == {"OK": 1900, "PENDENTE": 57}
        atlas_rows = [r for r in join_rows if r["ec"] == "1076801732"]
        assert len(atlas_rows) == 57
        assert all("Não faturar a Atlas" in r["erro"] for r in atlas_rows)


def check() -> None:
    check_parsing_markdown()
    check_parsing_structured_json()
    check_normalized_table_and_join()


if __name__ == "__main__":
    check()
