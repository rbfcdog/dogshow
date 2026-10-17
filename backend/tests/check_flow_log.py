import json
import os
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.application.cache_log import convert_cache_log
from src.application.normalization import normalize_reference
from src.domain.normalization import ContractProposal
from src.infrastructure.flow_log import FlowLog
from src.infrastructure.normalization_sources import digest, document_chunks, json_text
from src.infrastructure.openai_normalizer import ModelUnavailable, OpenAIInterpreter
from src.infrastructure.readers import load_dataset
from src.infrastructure.source_scope import SOURCE_SCOPE
from src.normalizar import main

from check_normalization import _EvidenceInterpreter, _ReferenceInterpreter


def read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def check_incremental_log() -> None:
    root = Path(__file__).resolve().parents[2]
    dataset = load_dataset(root / "dados_tarvo")
    with TemporaryDirectory() as directory:
        output = Path(directory)
        fake = _ReferenceInterpreter()
        original_invoke = fake.invoke

        def invoke(context, stage):
            records = read_log(output / "fluxo.log")
            assert records[-1]["event"] in {"CONTEXTO_SELECIONADO", "INVESTIGACAO_INICIADA"}
            assert not (output / "referencias.json").exists()
            return original_invoke(context, stage)

        with patch.object(fake, "invoke", side_effect=invoke):
            report = normalize_reference(dataset, root / "dados_tarvo", output, fake)
        records = read_log(output / "fluxo.log")
        assert records[0]["event"] == "EXECUCAO_INICIADA"
        assert records[-1]["event"] == "EXECUCAO_CONCLUIDA"
        assert len({r["run_id"] for r in records}) == 1
        assert [r["sequence"] for r in records] == list(range(1, len(records) + 1))
        assert [r["elapsed_ms"] for r in records] == sorted(r["elapsed_ms"] for r in records)
        assert all(datetime.fromisoformat(r["timestamp"]).utcoffset().total_seconds() == 0 for r in records)
        assert {r["stage"] for r in records} >= {"NORMALIZACAO", "JOIN", "RCA", "ESCALADA", "EXPORTACAO"}
        assert len([r for r in records if r["event"] == "PROPOSTA_VALIDADA"]) == report["summary"]["contract_rows"]
        extraction = next(r for r in records if r["event"] == "EXTRACAO_CONTRATUAL_CONCLUIDA")
        assert extraction["details"]["document_chunks"] == len(document_chunks(root / "dados_tarvo"))
        assert "docs" not in json.dumps(extraction["details"])
        concluded = [r for r in records if r["event"] == "INVESTIGACAO_CONCLUIDA"]
        assert [r["details"]["ec"] for r in concluded] == ["1038867654", "1043300981", "1043396662", "1076801732"]
        assert not any(r["details"]["resolved"] or r["details"]["applied_source_ids"] for r in concluded)
        escalations = {r["details"]["ec"]: r for r in records if r["event"] == "CASO_ESCALADO"}
        assert {ec: r["details"]["affected_rows"] for ec, r in escalations.items()} == {
            "1038867654": 85, "1043300981": 73, "1043396662": 75, "1076801732": 57,
        }
        assert all(r["level"] == "WARNING" and r["details"]["responsible"] == "Comercial / Financeiro"
            for r in escalations.values())
        summary = next(r for r in records if r["event"] == "RESUMO")["details"]
        assert summary == report["summary"] and summary["human_cases"] == 4 and summary["unresolved_rows"] == 290

        evidence_output = output / "evidencia"
        normalize_reference(dataset, root / "dados_tarvo", evidence_output, _EvidenceInterpreter())
        evidence_records = read_log(evidence_output / "fluxo.log")
        resolved = [r["details"]["ec"] for r in evidence_records
            if r["event"] == "INVESTIGACAO_CONCLUIDA" and r["details"]["resolved"]]
        assert resolved == ["1038867654", "1043300981", "1043396662"]
        escalation = [r for r in evidence_records if r["event"] == "CASO_ESCALADO"]
        assert len(escalation) == 1 and escalation[0]["details"]["ec"] == "1076801732"
        assert escalation[0]["details"]["affected_rows"] == 57

        class FailedInterpreter:
            model = "fake"

            def invoke(self, context, stage):
                raise RuntimeError("mensagem-sensivel-nao-registrar")

        try:
            normalize_reference(dataset, root / "dados_tarvo", output, FailedInterpreter())
        except RuntimeError:
            pass
        else:
            raise AssertionError("Falha simulada não propagada.")
        changed = read_log(output / "fluxo.log")
        assert changed[:len(records)] == records
        assert changed[-1]["event"] == "EXECUCAO_FALHOU"
        assert changed[-1]["details"]["error_type"] == "RuntimeError"
        assert changed[-1]["run_id"] != records[0]["run_id"]
        assert "mensagem-sensivel-nao-registrar" not in (output / "fluxo.log").read_text(encoding="utf-8")


def check_cache_events_and_conversion() -> None:
    proposal = ContractProposal(source_id="contratos!A6:L6", establishment_code=None,
        validity_start=None, validity_end=None, disposition="PENDENTE",
        evidence_ids=["contratos!A6:L6"], reason="Sem vigência comprovada.")
    with TemporaryDirectory() as directory, patch.dict(os.environ, {
        "OPENAI_API_KEY": "sk-chave-ficticia-do-teste", "OPENAI_MODEL": "modelo-fake",
    }, clear=True):
        root = Path(directory)
        output = root / "output"
        client = Mock()
        client.responses.parse.return_value = SimpleNamespace(output_parsed=proposal, id="resp-fake", usage=None)
        with patch("src.infrastructure.openai_normalizer.OpenAI", return_value=client):
            with FlowLog(output) as log:
                interpreter = OpenAIInterpreter(root / "ausente.env", output / "cache_llm", log=log)
                context = {"source_scope": SOURCE_SCOPE, "focal": {"source_id": proposal.source_id}}
                assert interpreter.invoke(context, "NORMALIZACAO") == proposal
                assert interpreter.invoke(context, "NORMALIZACAO") == proposal
                try:
                    interpreter.invoke({"focal": {"source_id": proposal.source_id}}, "NORMALIZACAO")
                except ModelUnavailable:
                    pass
                else:
                    raise AssertionError("Contexto sem escopo foi aceito.")
        assert client.responses.parse.call_count == 1
        records = read_log(output / "fluxo.log")
        details = next(r["details"] for r in records if r["event"] == "LLM_RESPOSTA_REGISTRADA")
        cached = next(r["details"] for r in records if r["event"] == "CACHE_REUTILIZADO")
        assert details["cache_record"] == cached["cache_record"]
        assert details["cache_record"]["response"] == proposal.model_dump()
        assert "system" in details["cache_record"]["request"]
        assert details["cache_record"]["request"]["source_scope"] == SOURCE_SCOPE
        # A chamada sem escopo é recusada antes de qualquer evento/solicitação ao modelo.
        assert [r["event"] for r in records].count("LLM_SOLICITACAO_INICIADA") == 2
        cache_files = list((output / "cache_llm").glob("*.json"))
        original_bytes = {p.name: p.read_bytes() for p in cache_files}
        request_hash = digest(json_text(details["cache_record"]["request"]).encode("utf-8"))
        assert request_hash == details["request_hash"] == cache_files[0].stem
        with patch(
            "src.infrastructure.openai_normalizer.OpenAI", side_effect=AssertionError("Conversão não chama API."),
        ):
            assert convert_cache_log(output / "cache_llm", output) == {"cache_files": 1, "converted": 1, "rejected": 0}
        converted = read_log(output / "fluxo.log")
        assert converted[:len(records)] == records
        imported = next(r for r in converted if r["event"] == "CACHE_CONVERTIDO")
        assert imported["details"]["cache_record"] == details["cache_record"]
        assert {p.name: p.read_bytes() for p in cache_files} == original_bytes
        assert os.environ["OPENAI_API_KEY"] not in (output / "fluxo.log").read_text(encoding="utf-8")
        (output / "cache_llm" / "quebrado.json").write_text("{", encoding="utf-8")
        legacy_record = json.loads(cache_files[0].read_text(encoding="utf-8"))
        for scope in (None, "dados_tarvo_e_docs_v1"):
            legacy_record["request"].pop("source_scope", None)
            if scope:
                legacy_record["request"]["source_scope"] = scope
            legacy_hash = digest(json_text(legacy_record["request"]).encode("utf-8"))
            (output / "cache_llm" / f"{legacy_hash}.json").write_text(json_text(legacy_record), encoding="utf-8")
        summary = convert_cache_log(output / "cache_llm", root / "conversion")
        assert summary == {"cache_files": 4, "converted": 1, "rejected": 3}
        conversion_log = read_log(root / "conversion/fluxo.log")
        rejected = {r["details"]["cache_file"] for r in conversion_log if r["event"] == "CONVERSAO_REJEITADA"}
        assert "quebrado.json" in rejected and cache_files[0].name not in rejected and len(rejected) == 3
        assert [r["details"]["request_hash"] for r in conversion_log if r["event"] == "CACHE_CONVERTIDO"] == [cache_files[0].stem]

        with patch.object(client.responses, "parse", side_effect=ValueError("conteudo-nao-registrar")):
            with patch("src.infrastructure.openai_normalizer.OpenAI", return_value=client):
                try:
                    with FlowLog(root / "failed") as log:
                        failed = OpenAIInterpreter(root / "ausente.env", root / "failed/cache_llm", log=log)
                        failed.invoke({"source_scope": SOURCE_SCOPE, "new": True}, "NORMALIZACAO")
                except ValueError:
                    pass
                else:
                    raise AssertionError("Erro do provedor não propagado.")
        failed_log = read_log(root / "failed/fluxo.log")
        assert any(r["event"] == "LLM_CHAMADA_FALHOU" and r["level"] == "ERROR" for r in failed_log)
        assert failed_log[-1]["event"] == "EXECUCAO_FALHOU"
        assert "conteudo-nao-registrar" not in (root / "failed/fluxo.log").read_text(encoding="utf-8")

        stdout = StringIO()
        with patch("sys.argv", ["normalizar", "--converter-cache", str(output / "cache_llm"),
            "--saida", str(root / "cli")]), redirect_stdout(stdout):
            main()
        assert json.loads(stdout.getvalue()) == summary


def check_cli_failure() -> None:
    with TemporaryDirectory() as directory:
        output = Path(directory) / "failed"
        with patch("sys.argv", ["normalizar", "--entrada", str(Path(directory) / "missing"),
            "--saida", str(output)]), patch("src.normalizar.OpenAIInterpreter", side_effect=ModelUnavailable("Configuração ausente.")), patch("sys.stderr", StringIO()):
            try:
                main()
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError("Falha de configuração foi ignorada.")
        records = read_log(output / "fluxo.log")
        assert records[0]["event"] == "EXECUCAO_INICIADA"
        assert records[-1]["event"] == "EXECUCAO_FALHOU"
        assert records[-1]["details"]["error_type"] == "ModelUnavailable"


def check() -> None:
    check_incremental_log()
    check_cache_events_and_conversion()
    check_cli_failure()


if __name__ == "__main__":
    check()
