import csv
import json
import os
import re
import shutil
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from openai import APIError
from pydantic import ValidationError

from src.application.closing import close_month
from src.application.normalization import (
    context_for,
    explicit_bounds,
    join_checkout,
    load_reference,
    materialize,
    normalize_reference,
    validate_proposal,
)
from src.domain.models import CheckoutEntry, Contract, DataPackError, Dataset, SourceRef
from src.domain.normalization import ContractProposal, Investigation
from src.domain.policies import active_on, contract_candidates, has_commercial_question
from src.infrastructure.normalization_sources import (
    contract_id,
    contract_rows,
    document_chunks,
    email_chunks,
    input_manifest,
    json_text,
    normalize_name,
    orbe_candidates,
    related_rows,
    similarity,
)
from src.infrastructure.openai_normalizer import ModelUnavailable, OpenAIInterpreter, SYSTEM_PROMPT
from src.infrastructure.readers import load_dataset
from src.infrastructure.source_scope import DATA_ROOT, SOURCE_SCOPE, require_data_dir, require_source_file
from src.normalizar import main as normalize_main

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "dados_tarvo"
UNBOUND_ECS = {"1038867654", "1043300981", "1043396662", "1076801732"}
SOURCE_FILES = {path.name for path in DATA_DIR.iterdir()
    if path.is_file() and path.suffix.lower() in {".csv", ".ofx", ".eml"}}


def _proposal() -> dict:
    return {
        "source_id": "contratos.xlsx!Taxas vigentes!A6:L6",
        "establishment_code": None,
        "validity_start": None,
        "validity_end": "2026-09-14",
        "disposition": "PENDENTE",
        "evidence_ids": ["contratos.xlsx!Taxas vigentes!A6:L6"],
        "reason": "Identidade ainda depende de confirmação documental.",
    }


def _reject(schema, payload: dict, location: tuple, kind: str) -> None:
    try:
        schema.model_validate(payload)
    except ValidationError as error:
        assert any(item["loc"] == location and item["type"] == kind for item in error.errors())
    else:
        raise AssertionError("Schema aceitou uma proposta inválida.")


def _check_schemas() -> None:
    payload = _proposal()
    assert ContractProposal.model_validate(payload).model_dump() == payload
    for field, value in (
        ("rates", {"PIX": "0"}),
        ("applied_rate", "0.01"),
        ("remittance_rate", "0.015"),
        ("cnpj", "00000000000000"),
        ("invented_field", True),
    ):
        _reject(ContractProposal, {**payload, field: value}, (field,), "extra_forbidden")
    _reject(ContractProposal, {**payload, "establishment_code": 123}, ("establishment_code",), "string_type")
    _reject(ContractProposal, {**payload, "disposition": "CONFIRMADO"}, ("disposition",), "literal_error")
    _reject(ContractProposal, {**payload, "reason": ""}, ("reason",), "string_too_short")
    missing = {key: value for key, value in payload.items() if key != "validity_start"}
    _reject(ContractProposal, missing, ("validity_start",), "missing")

    investigation = {
        "diagnosis": "Join sem confirmação suficiente.",
        "hypotheses": ["Nome abreviado na Orbe."],
        "evidence_ids": payload["evidence_ids"],
        "proposals": [payload],
        "missing_information": "Confirmação do EC pelo financeiro.",
    }
    assert Investigation.model_validate(investigation).proposals[0].model_dump() == payload
    assert Investigation.model_validate({**investigation, "proposals": []}).proposals == []
    _reject(Investigation, {**investigation, "rates": {}}, ("rates",), "extra_forbidden")
    _reject(
        Investigation, {**investigation, "proposals": [{**payload, "rates": {"PIX": "0"}}]},
        ("proposals", 0, "rates"), "extra_forbidden",
    )
    for schema in (ContractProposal, Investigation):
        assert schema.model_json_schema()["additionalProperties"] is False
    assert Investigation.model_json_schema()["$defs"]["ContractProposal"]["additionalProperties"] is False


def _check_candidates() -> None:
    assert normalize_name("  São-JOÃO, Educação & Câmbio S.A.  ") == "sao joao educacao cambio s a"
    assert similarity("Ponte Câmbio S.A.", "PONTE CAMBIO S A") == 100
    assert similarity("São João Educação Ltda", "SAO JOAO EDUCACAO LTDA") == 100
    assert similarity("Bridge Idiomas", "BRIDGE IDIOMAS LTDA") >= 85
    assert similarity("Bridge Idiomas", "Oficina Mecânica Horizonte") < 85

    focal = {
        "source_id": "focal", "cnpj": "66564512000199",
        "establishment_code": "EC-EXATO", "merchant": "São João Educação Ltda",
    }
    rows = [
        {**focal, "source_id": "nome", "cnpj": "11111111000111", "establishment_code": None,
            "merchant": "SAO JOAO EDUCACAO LTDA"},
        {**focal, "source_id": "ec", "cnpj": "22222222000122", "merchant": "Oficina Horizonte"},
        {**focal, "source_id": "cnpj", "establishment_code": None, "merchant": "Mercado Azul"},
        {**focal, "source_id": "fora", "cnpj": "33333333000133", "establishment_code": None,
            "merchant": "Clínica Veterinária"},
    ]
    matched = related_rows(focal, rows, threshold=90, limit=10)
    assert {row["source_id"] for row in matched} == {"nome", "ec", "cnpj"}
    assert {row["source_id"] for row in related_rows(focal, rows, 90, 2)} == {"ec", "cnpj"}
    assert related_rows(focal, list(reversed(rows)), 90, 10) == matched
    assert related_rows(focal, rows, 90, 0) == []

    base = CheckoutEntry(
        source=SourceRef("orbe.csv", "T1", 2), occurred_on=date(2026, 9, 1),
        establishment_code="EC-EXATO", merchant="Oficina Horizonte", kind="VENDA",
        modality="CREDITO A VISTA", installments=1, gross=Decimal("100"),
        applied_rate=Decimal("0.0269"), fee=Decimal("2.69"), net=Decimal("97.31"),
        original_ref=None, original_on=None,
    )
    entries = tuple(
        replace(base, establishment_code=ec, merchant=name, occurred_on=date(2026, 9, day),
            source=SourceRef("orbe.csv", ref, row))
        for ec, name in (
            ("EC-EXATO", "Oficina Horizonte"),
            ("1043300981", "Mercado Azul"),
            ("EC-NOME", "SAO JOAO EDUCACAO LTDA"),
            ("EC-FORA", "Clínica Veterinária"),
        )
        for row, day, ref in ((4, 2, "T3"), (3, 1, "T2"), (2, 1, "T1"))
    )
    dataset = Dataset(entries, (), (), (), (), {}, {})
    selected = orbe_candidates(focal, dataset, threshold=90, sample_limit=2)
    by_ec = {candidate["establishment_code"]: candidate for candidate in selected}
    # 1043300981 não entra por CNPJ: a Orbe não tem coluna de CNPJ e não existem vínculos externos.
    assert set(by_ec) == {"EC-EXATO", "EC-NOME"}
    assert by_ec["EC-NOME"]["name_score"] == 100
    for candidate in selected:
        assert set(candidate) == {"establishment_code", "names", "name_score", "total_rows", "samples"}
        assert candidate["total_rows"] == 3
        assert [sample["source"]["ref"] for sample in candidate["samples"]] == ["T1", "T2"]
        assert {sample["establishment_code"] for sample in candidate["samples"]} == {candidate["establishment_code"]}
    assert orbe_candidates(focal, replace(dataset, checkout=tuple(reversed(entries))), 90, 2) == selected
    assert all(len(candidate["samples"]) == 1 for candidate in orbe_candidates(focal, dataset, 90, 1))
    other_cnpj = {**focal, "cnpj": "99999999000199"}
    assert orbe_candidates(other_cnpj, dataset, 90, 2) == selected
    by_name = {**focal, "merchant": "Mercado Azul", "establishment_code": None}
    assert {c["establishment_code"] for c in orbe_candidates(by_name, dataset, 90, 2)} == {"1043300981"}


def _check_validity() -> None:
    contract = Contract(
        SourceRef("contratos.xlsx", "A6:L6", 6, "Taxas vigentes"),
        "Lojista", "00000000000100", "123", {"PIX": Decimal("0.01")}, None,
        "texto humano não interpretável", None, date(2026, 9, 10), date(2026, 9, 14), True,
    )
    for day, expected in ((9, False), (10, True), (12, True), (14, True), (15, False)):
        assert active_on(contract, date(2026, 9, day)) is expected
    end_only = replace(contract, validity_start=None)
    assert active_on(end_only, date(2025, 1, 1))
    assert active_on(end_only, date(2026, 9, 14))
    assert not active_on(end_only, date(2026, 9, 15))
    start_only = replace(contract, validity_end=None)
    assert not active_on(start_only, date(2026, 9, 9))
    assert active_on(start_only, date(2026, 9, 10))
    assert active_on(start_only, date(2027, 1, 1))
    unknown = replace(contract, validity_start=None, validity_end=None, validity="01/01/2020")
    assert not active_on(unknown, date(2026, 9, 12))
    assert not active_on(replace(contract, note="Cadastro inativo"), date(2026, 9, 12))
    legacy = replace(contract, structured_validity=False, validity="até 14/09/2026")
    assert active_on(legacy, date(2026, 9, 14))
    assert not active_on(legacy, date(2026, 9, 15))
    for validity in ("15/09/2026", "a partir de 15/09/2026"):
        legacy = replace(legacy, validity=validity)
        assert not active_on(legacy, date(2026, 9, 14))
        assert active_on(legacy, date(2026, 9, 15))


def _check_real_contracts() -> None:
    data_dir = Path(__file__).resolve().parents[2] / "dados_tarvo"
    dataset = load_dataset(data_dir)
    rows = contract_rows(data_dir, dataset)
    current = [row for row in rows if not row["historical"]]
    historical = [row for row in rows if row["historical"]]
    assert len(current) == len(dataset.contracts)
    assert {row["source_id"] for row in current} == {contract_id(contract) for contract in dataset.contracts}
    assert historical and all("!ANTIGO 2025 - nao usar!" in row["source_id"] for row in historical)
    assert all(row["establishment_code"] is None and row["validity"] is None for row in historical)
    assert not {row["source_id"] for row in historical} & {contract_id(contract) for contract in dataset.contracts}
    atlas = [row for row in historical if "atlas" in row["merchant"].casefold()]
    assert atlas
    assert all(not contract_candidates(dataset.contracts, date(2026, 9, 15), cnpj=row["cnpj"]) for row in atlas)

    casa = next(row for row in current if row["source_id"].endswith("!A13:L13"))
    assert casa["cnpj"] == "00520815000100"
    assert "conversar com Diego (comercial) sobre condição especial" in casa["cells"]["A13"]["comment"]
    assert casa["cells"]["L13"]["value"] == casa["note"] == "cliente estratégico - ver c/ comercial"
    assert set(casa["cells"]) == {f"{column}13" for column in "ABCDEFGHIJKL"}
    for cell in casa["cells"].values():
        assert set(cell) == {"value", "format", "type", "comment"}
    casa_contract = next(contract for contract in dataset.contracts if contract.source.ref == "A13:L13")
    assert casa_contract.comments == casa["cells"]["A13"]["comment"]
    assert casa_contract.rates and all(rate > 0 for rate in casa_contract.rates.values())
    assert has_commercial_question(casa_contract)
    questioned = {contract.source.ref for contract in dataset.contracts if has_commercial_question(contract)}
    assert questioned == {"A13:L13"}
    assert all(contract.comments is None for contract in dataset.contracts if contract is not casa_contract)
    terms = dataset.partner_terms
    assert terms is not None and terms.participation == Decimal("0.5")
    assert terms.cnpj == "75374179000146" and terms.name == "Ponte Câmbio S.A."
    assert terms.source == SourceRef("03_email_ponte_cambio_relatorio_set2026.eml", "MIME:text/plain")
    standard = {contract.source.ref for contract in dataset.contracts if contract.remittance_rate is None}
    assert {"A6:L6", "A7:L7", "A8:L8", "A9:L9", "A16:L16"} <= standard
    assert next(c for c in dataset.contracts if c.source.ref == "A15:L15").remittance_rate == Decimal("0.012")
    unbound = {contract.cnpj for contract in dataset.contracts if contract.establishment_code is None}
    assert {"78576234000179", "07752116000126", "66564512000199"} <= unbound
    assert not UNBOUND_ECS & {contract.establishment_code for contract in dataset.contracts}

    bridge = [row for row in current if row["cnpj"] == "22224579000109"]
    assert len(bridge) == 2
    assert {row["source_id"].rsplit("!", 1)[1] for row in bridge} == {"A6:L6", "A7:L7"}
    focal = {**bridge[0], "merchant": "Nome divergente", "establishment_code": None}
    assert {row["source_id"] for row in related_rows(focal, rows, 100, len(rows))} == {
        row["source_id"] for row in rows if row["cnpj"] == focal["cnpj"]
    }
    contracts = tuple(contract for contract in dataset.contracts if contract.cnpj == focal["cnpj"])
    before = contract_candidates(contracts, date(2026, 9, 14), cnpj=focal["cnpj"])
    after = contract_candidates(contracts, date(2026, 9, 15), cnpj=focal["cnpj"])
    assert len(before) == len(after) == 1
    assert before[0].source.row == 6 and before[0].rates["CREDITO A VISTA"] == Decimal("0.0259")
    assert after[0].source.row == 7 and after[0].rates["CREDITO A VISTA"] == Decimal("0.0219")
    structured = (
        replace(before[0], structured_validity=True, validity_end=date(2026, 9, 14)),
        replace(after[0], structured_validity=True, validity_start=date(2026, 9, 15)),
    )
    assert contract_candidates(structured, date(2026, 9, 14), cnpj=focal["cnpj"]) == (structured[0],)
    assert contract_candidates(structured, date(2026, 9, 15), cnpj=focal["cnpj"]) == (structured[1],)


def _check_interpreter() -> None:
    proposal = ContractProposal.model_validate(_proposal())
    investigation = Investigation(
        diagnosis="Evidência insuficiente.", hypotheses=[], evidence_ids=[proposal.source_id],
        proposals=[], missing_information="Confirmação do financeiro.",
    )
    usage = {"input_tokens": 150, "output_tokens": 30, "total_tokens": 180}
    response = SimpleNamespace(output_parsed=proposal, id="resp-ficticia", usage=Mock())
    response.usage.model_dump.return_value = usage
    with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
        root = Path(directory)
        env_file = root / "mock.env"
        fake_key = "sk-ficticia-apenas-teste-sem-api"
        env_file.write_text(f"OPENAI_API_KEY={fake_key}\nOPENAI_MODEL=modelo-ficticio-teste\n", encoding="utf-8")
        client = Mock()
        client.responses.parse.return_value = response
        with patch("src.infrastructure.openai_normalizer.OpenAI", return_value=client) as factory:
            interpreter = OpenAIInterpreter(env_file, root / "cache")
            assert interpreter.model == "modelo-ficticio-teste"
            factory.assert_called_once_with(timeout=120, max_retries=2)
            context = {
                "source_scope": SOURCE_SCOPE,
                "focal": {"source_id": proposal.source_id}, "candidates": [],
                "sample": {"rate": Decimal("0.0269"), "on": date(2026, 9, 14)},
            }
            unscoped = {key: value for key, value in context.items() if key != "source_scope"}
            for invalid in (unscoped, {**context, "source_scope": "docs"}, {**context, "source_scope": None},
                    {**context, "source_scope": "dados_tarvo_only_v1"}):
                for stage in ("NORMALIZACAO", "RCA"):
                    try:
                        interpreter.invoke(invalid, stage)
                    except ModelUnavailable as error:
                        assert "escopo" in str(error) and "recusada" in str(error)
                    else:
                        raise AssertionError("Contexto sem escopo dados_tarvo foi enviado ao modelo.")
            client.responses.parse.assert_not_called()
            assert not (root / "cache").exists()
            assert interpreter.invoke(context, "NORMALIZACAO") == proposal
            client.responses.parse.assert_called_once_with(
                model="modelo-ficticio-teste",
                input=[{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json_text({"stage": "NORMALIZACAO", **context})}],
                text_format=ContractProposal,
            )
            reordered = dict(reversed(list(context.items())))
            assert interpreter.invoke(reordered, "NORMALIZACAO") == proposal
            restarted = OpenAIInterpreter(env_file, root / "cache")
            assert restarted.invoke(context, "NORMALIZACAO") == proposal
            assert client.responses.parse.call_count == 1
            cache_files = list((root / "cache").glob("*.json"))
            assert len(cache_files) == 1
            record = json.loads(cache_files[0].read_text(encoding="utf-8"))
            assert record["usage"] == usage and record["response_id"] == "resp-ficticia"
            assert record["response"] == proposal.model_dump()
            assert record["request"]["schema"] == ContractProposal.model_json_schema()
            assert record["request"]["source_scope"] == SOURCE_SCOPE == "dados_tarvo_only_v2"
            assert record["request"]["context"]["source_scope"] == SOURCE_SCOPE
            assert "SOMENTE" in record["request"]["system"] and "dados_tarvo" in record["request"]["system"]
            assert record["request"]["context"]["sample"] == {"rate": "0.0269", "on": "2026-09-14"}

            assert interpreter.invoke({**context, "extra_evidence": ["doc:L1"]}, "NORMALIZACAO") == proposal
            response.output_parsed = investigation
            assert interpreter.invoke(context, "RCA") == investigation
            assert client.responses.parse.call_args.kwargs["text_format"] is Investigation
            os.environ["OPENAI_MODEL"] = "outro-modelo-ficticio"
            different_model = OpenAIInterpreter(env_file, root / "cache")
            response.output_parsed = proposal
            assert different_model.invoke(context, "NORMALIZACAO") == proposal
            assert client.responses.parse.call_count == 4
            assert client.responses.parse.call_args.kwargs["model"] == "outro-modelo-ficticio"
            assert len(list((root / "cache").glob("*.json"))) == 4
            for path in (root / "cache").iterdir():
                assert path.suffix == ".json"
                content = path.read_text(encoding="utf-8")
                assert fake_key not in content and "OPENAI_API_KEY" not in content

            record["request"]["model"] = "cache-adulterado"
            cache_files[0].write_text(json_text(record), encoding="utf-8")
            with patch.object(client.responses, "parse", side_effect=AssertionError("Cache deve ser validado.")) as parse:
                try:
                    interpreter.invoke(context, "NORMALIZACAO")
                except ModelUnavailable as error:
                    assert "Cache incompatível" in str(error)
                else:
                    raise AssertionError("Cache incompatível foi aceito.")
                parse.assert_not_called()

            failed = OpenAIInterpreter(env_file, root / "failed-cache")
            response.output_parsed = None
            try:
                failed.invoke(context, "NORMALIZACAO")
            except ModelUnavailable as error:
                assert "saída estruturada" in str(error)
            else:
                raise AssertionError("Saída estruturada ausente foi aceita.")
            assert not failed.cache_dir.exists()
            client.responses.parse.side_effect = APIError("erro simulado", request=Mock(), body=None)
            try:
                failed.invoke(context, "NORMALIZACAO")
            except ModelUnavailable as error:
                assert "Sem fallback" in str(error) and "APIError" in str(error)
                assert fake_key not in str(error)
            else:
                raise AssertionError("Erro da API não foi convertido em indisponibilidade.")
            assert not failed.cache_dir.exists()
            client.responses.parse.side_effect = None
            response.output_parsed = proposal
            fresh = OpenAIInterpreter(env_file, root / "cache", reuse_cache=False)
            before = client.responses.parse.call_count
            assert fresh.invoke(context, "NORMALIZACAO") == proposal
            assert fresh.invoke(context, "NORMALIZACAO") == proposal
            assert client.responses.parse.call_count == before + 2

        os.environ.pop("OPENAI_API_KEY")
        with patch("src.infrastructure.openai_normalizer.OpenAI") as factory:
            try:
                OpenAIInterpreter(root / "ausente.env", root / "unused-cache")
            except ModelUnavailable as error:
                assert "OPENAI_API_KEY ausente" in str(error)
            else:
                raise AssertionError("Cliente sem chave foi criado.")
            factory.assert_not_called()


def _proposal_for_row(row: dict, *, ec: str | None = None, evidence: tuple[str, ...] = ()) -> ContractProposal:
    disposition = "INATIVO" if "INATIVO" in (row["note"] or "").upper() else (
        "UTILIZAVEL" if row["validity"] else "PENDENTE"
    )
    start, end = explicit_bounds(row["validity"]) if row["validity"] else (None, None)
    return ContractProposal(
        source_id=row["source_id"], establishment_code=row["establishment_code"] or ec,
        validity_start=start.isoformat() if start else None,
        validity_end=end.isoformat() if end else None,
        disposition=disposition, evidence_ids=[row["source_id"], *evidence],
        reason="Proposta de teste baseada exclusivamente na linha contratual e no contexto recebido.",
    )


def _assert_scoped(context: dict) -> None:
    assert context["source_scope"] == SOURCE_SCOPE
    for document in context.get("documents", []):
        assert document["file"] in SOURCE_FILES and document["source_id"].startswith(document["file"])
    if "searched_documents" in context:
        assert set(context["searched_documents"]) == SOURCE_FILES


class _ReferenceInterpreter:
    """Fake conservador: preserva o EC da planilha e nunca deduz vínculo; RCA só diagnostica."""

    model = "interpretador-fake-sem-api"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, context: dict, stage: str) -> ContractProposal | Investigation:
        _assert_scoped(context)
        self.calls.append((stage, context))
        focal = context["focal"]
        if stage == "NORMALIZACAO":
            assert not focal["historical"]
            proposal = _proposal_for_row(focal)
            if focal["source_id"].endswith("!A6:L6"):
                proposal = proposal.model_copy(update={"validity_end": "14/09/2026"})
            return proposal
        assert stage == "RCA" and focal["establishment_code"] in UNBOUND_ECS
        return Investigation(
            diagnosis=f"EC {focal['establishment_code']} sem linha contratual atual com o mesmo EC.",
            hypotheses=["Vínculo por nome não comprovado nas fontes."],
            evidence_ids=[focal["source_id"], *(row["source_id"] for row in context["related_contracts"])],
            proposals=[], missing_information=f"Comercial deve confirmar contrato do EC {focal['establishment_code']}.",
        )


class _EvidenceInterpreter(_ReferenceInterpreter):
    """Na RCA, propõe o EC investigado apenas para uma única linha atual sem EC cujo nome
    corresponde ao nome Orbe presente no próprio contexto (inferência, não confirmação)."""

    model = "interpretador-fake-evidencia"

    def invoke(self, context: dict, stage: str) -> ContractProposal | Investigation:
        if stage != "RCA":
            return super().invoke(context, stage)
        _assert_scoped(context)
        self.calls.append((stage, context))
        focal, ec = context["focal"], context["focal"]["establishment_code"]
        groups = {group["establishment_code"]: group for group in context["orbe_groups"]}
        assert ec in groups
        candidates = [row for row in context["related_contracts"]
            if not row["historical"] and row["establishment_code"] is None
            and max(similarity(row["merchant"], name) for name in groups[ec]["names"]) >= 90]
        if len(candidates) != 1:
            return Investigation(
                diagnosis=f"Nenhuma linha atual sem EC corresponde ao nome Orbe do EC {ec}.",
                hypotheses=[], evidence_ids=[focal["source_id"]], proposals=[],
                missing_information=f"Comercial deve confirmar contrato do EC {ec}.",
            )
        sample = groups[ec]["samples"][0]["source"]
        evidence = (focal["source_id"], f"{sample['file']}!{sample['ref']}!row{sample['row']}")
        proposal = _proposal_for_row(candidates[0], ec=ec, evidence=evidence)
        return Investigation(
            diagnosis=f"Nome Orbe do EC {ec} corresponde a linha atual sem EC; vínculo inferido.",
            hypotheses=["Inferência por nome; validar com o Comercial."],
            evidence_ids=list(proposal.evidence_ids), proposals=[proposal], missing_information="",
        )


def _reject_proposal(proposal: ContractProposal, context: dict, rows: list[dict], message: str) -> None:
    try:
        validate_proposal(proposal, context, rows)
    except ValueError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError("Proposta sem suporte documental foi aceita.")


def _check_proposal_validation() -> None:
    data_dir = Path(__file__).resolve().parents[2] / "dados_tarvo"
    dataset = load_dataset(data_dir)
    rows = contract_rows(data_dir, dataset)
    current = [row for row in rows if not row["historical"]]
    for row in current:
        if not row["validity"]:
            continue
        expected = (None, date(2026, 9, 14)) if row["source_id"].endswith("!A6:L6") else (
            (date(2026, 9, 15), None) if row["source_id"].endswith("!A7:L7") else (
                (date(2026, 6, 1), None) if row["source_id"].endswith(("!A18:L18", "!A28:L28"))
                else (date(2026, 1, 1), None)
            )
        )
        assert explicit_bounds(row["validity"]) == expected
    assert explicit_bounds("  ATÉ 14/09/2026  ") == (None, date(2026, 9, 14))
    for validity in (None, "", "setembro/2026", "31/02/2026"):
        try:
            explicit_bounds(validity)
        except ValueError:
            pass
        else:
            raise AssertionError("Vigência sem data explícita válida foi aceita.")

    row = next(row for row in current if row["source_id"].endswith("!A6:L6"))
    context = context_for(row, rows, dataset, 75, 2, 8)
    proposal = _proposal_for_row(row)
    validate_proposal(proposal, context, rows)
    validate_proposal(proposal.model_copy(update={"validity_end": "14/09/2026"}), context, rows)
    sample = context["orbe_groups"][0]["samples"][0]["source"]
    compound = f"{sample['file']}!{sample['ref']}!row{sample['row']}"
    rca_context = {**context, "focal": {**context["focal"], "source_id": "orbe:EC:1075624862"}}
    validate_proposal(proposal.model_copy(update={
        "evidence_ids": [proposal.source_id, rca_context["focal"]["source_id"], compound],
    }), rca_context, rows)
    _reject_proposal(proposal.model_copy(update={"validity_end": "2026-09-15"}), context, rows,
        "Vigência proposta não corresponde")
    _reject_proposal(proposal.model_copy(update={"validity_start": "2026-01-01"}), context, rows,
        "Vigência proposta não corresponde")
    _reject_proposal(proposal.model_copy(update={"validity_end": "2026-02-30"}), context, rows,
        "Datas propostas inválidas")
    _reject_proposal(proposal.model_copy(update={"evidence_ids": [proposal.source_id, "fonte-inexistente"]}),
        context, rows, "evidência inexistente")
    _reject_proposal(proposal.model_copy(update={"establishment_code": "EC-INVENTADO"}), context, rows,
        "altera um EC explicitamente preenchido")
    historical = next(row for row in rows if row["historical"])
    historical_proposal = proposal.model_copy(update={
        "source_id": historical["source_id"], "evidence_ids": [historical["source_id"]],
    })
    historical_context = {**context, "related_contracts": [*context["related_contracts"], historical]}
    _reject_proposal(historical_proposal, historical_context, rows, "linha contratual atual")
    altered_context = {**context, "related_contracts": [
        {**candidate, "merchant": "Evidência adulterada"} if candidate["source_id"] == proposal.source_id else candidate
        for candidate in context["related_contracts"]
    ]}
    _reject_proposal(proposal, altered_context, rows, "não corresponde à extração dos originais")
    inactive = next(row for row in current if "INATIVO" in (row["note"] or "").upper())
    inactive_proposal = _proposal_for_row(inactive)
    assert inactive_proposal.disposition == "INATIVO"
    _reject_proposal(inactive_proposal.model_copy(update={"disposition": "UTILIZAVEL"}),
        context_for(inactive, rows, dataset, 75, 2, 8), rows, "marcado inativo")
    missing = {**row, "validity": None, "note": None}
    assert _proposal_for_row(missing).disposition == "PENDENTE"

    horizonte = next(row for row in current if row["source_id"].endswith("!A8:L8"))
    assert horizonte["establishment_code"] is None
    horizonte_context = context_for(horizonte, rows, dataset, 75, 2, 8)
    assert [group["establishment_code"] for group in horizonte_context["orbe_groups"]] == ["1043396662"]
    assert horizonte_context["source_scope"] == SOURCE_SCOPE
    assert horizonte_context["selection"]["orbe_has_cnpj_column"] is False
    validate_proposal(_proposal_for_row(horizonte), horizonte_context, rows)
    _reject_proposal(_proposal_for_row(horizonte, ec="1043300981"), horizonte_context, rows,
        "EC inferido não foi encontrado")
    _reject_proposal(_proposal_for_row(horizonte, ec="1075624862"), horizonte_context, rows,
        "EC inferido não foi encontrado")


def _expect_error(error_type: type[Exception], message: str, action) -> None:
    try:
        action()
    except error_type as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError(f"Esperado {error_type.__name__}: {message}")


def _check_source_scope() -> None:
    assert DATA_ROOT == DATA_DIR and require_data_dir(DATA_DIR) == DATA_DIR.resolve()
    assert require_data_dir(ROOT / "docs" / ".." / "dados_tarvo") == DATA_DIR.resolve()
    rejected = "Fonte de dados não permitida"
    for invalid in (ROOT / "docs", ROOT, ROOT / "backend", DATA_DIR / "inexistente"):
        _expect_error(DataPackError, rejected, lambda path=invalid: load_dataset(path))
        _expect_error(DataPackError, rejected, lambda path=invalid: require_data_dir(path))
    dataset = load_dataset(DATA_DIR)
    with TemporaryDirectory() as directory:
        copy = Path(directory) / "dados_tarvo"
        shutil.copytree(DATA_DIR, copy)
        assert input_manifest(DATA_DIR) == {f"dados/{path.name}": ANY for path in copy.iterdir()}
        for action in (
            lambda: load_dataset(copy), lambda: contract_rows(copy, dataset),
            lambda: document_chunks(copy), lambda: input_manifest(copy),
        ):
            _expect_error(DataPackError, rejected, action)
        for source in (copy / "01_orbe_relatorio_comissoes_set2026.csv", ROOT / "docs" / "00_LEIA-ME.md",
                ROOT / "backend" / "pyproject.toml"):
            _expect_error(DataPackError, "fora do pacote", lambda path=source: require_source_file(path))
        assert require_source_file(DATA_DIR / "04_plano_de_contas_fenix_erp.csv").name == "04_plano_de_contas_fenix_erp.csv"
        _expect_error(DataPackError, "fora do pacote", lambda: require_source_file(DATA_DIR / "inexistente.csv"))
        link = Path(directory) / "link"
        try:
            link.symlink_to(DATA_DIR, target_is_directory=True)
        except OSError:
            pass
        else:
            _expect_error(DataPackError, rejected, lambda: load_dataset(link))

        class _NeverCalled:
            model = "nao-pode-ser-chamado"

            def invoke(self, context, stage):
                raise AssertionError("Interpretador chamado com fonte fora do escopo.")

        output = Path(directory) / "saida"
        _expect_error(DataPackError, rejected,
            lambda: normalize_reference(dataset, copy, output, _NeverCalled()))
        assert not (output / "referencias.json").exists()
        log = (output / "fluxo.log").read_text(encoding="utf-8")
        assert '"EXECUCAO_FALHOU"' in log and '"DataPackError"' in log

    chunks = document_chunks(DATA_DIR)
    assert {chunk["file"] for chunk in chunks} == SOURCE_FILES
    assert not any(chunk["file"].endswith(".xlsx") or chunk["file"].endswith(".md") for chunk in chunks)
    email = "03_email_ponte_cambio_relatorio_set2026.eml"
    attachment = [chunk for chunk in chunks if chunk["source_id"].startswith(f"{email}!PonteCambio_Relatorio_TARVO_092026.xlsx!")]
    body = [chunk for chunk in chunks if chunk["source_id"].startswith(f"{email}:MIME:text/plain:")]
    assert attachment and body
    assert any("PC-0926-0162" in chunk["text"] for chunk in attachment)
    assert any("participação de 50%" in chunk["text"] for chunk in body)
    assert all(re.fullmatch(r".+:L\d+(-L\d+)?", chunk["source_id"]) for chunk in chunks)
    assert len({chunk["source_id"] for chunk in chunks}) == len(chunks)
    _check_late_adjustments_role()
    _check_duplicate_attachment_chunks()


def _unscoped(path: Path) -> Path:
    return path.resolve()


def _check_late_adjustments_role() -> None:
    """06_orbe_ajustes_tardios é obrigatório, único e com o layout do relatório 01."""
    full = load_dataset(DATA_DIR)
    late = [entry for entry in full.checkout if entry.source.file.startswith("06_orbe_ajustes_tardios_")]
    assert len(late) == 14 and len(full.checkout) == 1957
    with TemporaryDirectory() as directory, \
            patch("src.infrastructure.readers.require_data_dir", _unscoped), \
            patch("src.infrastructure.readers.require_source_file", _unscoped):
        copy = Path(directory) / "pacote"
        shutil.copytree(DATA_DIR, copy)
        adjustments = copy / "06_orbe_ajustes_tardios_set2026.csv"
        shutil.copy(adjustments, copy / "06_orbe_ajustes_tardios_set2026_v2.csv")
        _expect_error(DataPackError, "checkout_adjustments: esperado exatamente um arquivo 06_orbe_ajustes_tardios_*.csv; encontrados 2",
            lambda: load_dataset(copy))
        (copy / "06_orbe_ajustes_tardios_set2026_v2.csv").unlink()
        adjustments.write_text("id;outro\n", encoding="utf-8")
        _expect_error(DataPackError, "Cabeçalho", lambda: load_dataset(copy))
        adjustments.unlink()
        _expect_error(DataPackError, "checkout_adjustments: esperado exatamente um arquivo 06_orbe_ajustes_tardios_*.csv; encontrados 0",
            lambda: load_dataset(copy))


def _check_duplicate_attachment_chunks() -> None:
    """Dois anexos XLSX de mesmo nome não podem gerar source_id repetido."""
    from email.message import EmailMessage
    from io import BytesIO

    from openpyxl import Workbook

    with TemporaryDirectory() as directory, \
            patch("src.infrastructure.normalization_sources.require_data_dir", _unscoped), \
            patch("src.infrastructure.normalization_sources.require_source_file", _unscoped):
        folder = Path(directory)
        message = EmailMessage()
        message["Subject"] = "Relatórios"
        message.set_content("Corpo com participação de 50%.")
        for value in ("primeiro", "segundo"):
            workbook = Workbook()
            workbook.active.append([value])
            stream = BytesIO()
            workbook.save(stream)
            message.add_attachment(stream.getvalue(), maintype="application",
                subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="relatorio.xlsx")
        (folder / "03_email.eml").write_bytes(message.as_bytes())
        chunks = document_chunks(folder)
        identifiers = [chunk["source_id"] for chunk in chunks]
        assert len(identifiers) == len(set(identifiers)) == 3
        texts = {chunk["source_id"]: chunk["text"] for chunk in chunks}
        assert texts["03_email.eml!relatorio.xlsx!Sheet:L1"] == '["primeiro"]'
        assert texts["03_email.eml!relatorio.xlsx#anexo2!Sheet:L1"] == '["segundo"]'
        assert any(key.startswith("03_email.eml:MIME:text/plain:") for key in texts)


def _run_cli(argv: list[str]) -> tuple[int, str]:
    with patch("sys.argv", ["normalizar", *argv]), patch("sys.stderr", StringIO()) as stderr:
        try:
            normalize_main()
        except SystemExit as error:
            return error.code, stderr.getvalue()
    raise AssertionError("CLI deveria ter falhado.")


def _check_reference_pipeline() -> None:
    data_dir = DATA_DIR
    dataset = load_dataset(data_dir)
    rows = contract_rows(data_dir, dataset)
    originals = {contract_id(contract): contract for contract in dataset.contracts}
    current = [row for row in rows if not row["historical"]]
    fake = _ReferenceInterpreter()
    with TemporaryDirectory() as directory, patch(
        "src.infrastructure.openai_normalizer.OpenAI", side_effect=AssertionError("API real proibida no teste."),
    ):
        output = Path(directory) / "referencia"
        assert data_dir.resolve() not in output.resolve().parents
        report = normalize_reference(dataset, data_dir, output, fake, samples=2)
        assert report["version"] == 2 and report["source_scope"] == SOURCE_SCOPE and report["model"] == fake.model
        assert report["settings"] == {"samples": 2, "threshold": 75, "candidate_limit": 8}
        assert set(report["input_hashes"]) == {f"dados/{path.name}" for path in data_dir.iterdir() if path.is_file()}
        normalizations = {item["source_id"]: item for item in report["normalization"]}
        assert set(normalizations) == set(originals)
        assert all(item["validation_error"] is None for item in normalizations.values())
        assert Counter(stage for stage, _ in fake.calls) == {"NORMALIZACAO": len(current), "RCA": 4}
        rca_calls = [context for stage, context in fake.calls if stage == "RCA"]
        assert [context["focal"]["establishment_code"] for context in rca_calls] == sorted(UNBOUND_ECS)
        for stage, context in fake.calls:
            assert context["source_scope"] == SOURCE_SCOPE
            assert all(len(group["samples"]) <= 2 for group in context["orbe_groups"])
            if stage == "NORMALIZACAO":
                assert context["selection"]["samples_per_ec"] == 2
        for context in rca_calls:
            assert context["focal"]["cnpj"] == "" and context["documents"]
            assert all(context["focal"]["establishment_code"] in re.sub(r"\D", "", doc["text"])
                for doc in context["documents"])

        horizonte = next(row for row in current if row["merchant"] == "Horizonte Viagens")
        horizonte_id = horizonte["source_id"]
        assert normalizations[horizonte_id]["proposal"]["disposition"] == "UTILIZAVEL"
        assert report["decisions"][horizonte_id]["proposal"]["establishment_code"] is None
        assert all(decision["proposal"]["establishment_code"] == originals[source_id].establishment_code
            for source_id, decision in report["decisions"].items())
        inactive_ids = {row["source_id"] for row in current if "INATIVO" in (row["note"] or "").upper()}
        assert set(report["unusable_contracts"]) == inactive_ids
        assert all(normalizations[source_id]["proposal"]["disposition"] == "INATIVO" for source_id in inactive_ids)

        restored = load_reference(dataset, data_dir, output / "referencias.json")
        rematerialized, canonical = materialize(dataset, report["decisions"], rows)
        assert restored == rematerialized
        assert len(restored.contracts) == len(originals) - len(inactive_ids)
        for contract in restored.contracts:
            raw = originals[contract_id(contract)]
            assert contract.structured_validity
            assert (contract.validity_start, contract.validity_end) == explicit_bounds(raw.validity)
            assert contract.rates == raw.rates and contract.remittance_rate == raw.remittance_rate
            assert contract.source.sheet == "Taxas vigentes"
        assert len([contract for contract in restored.contracts if contract.cnpj == "22224579000109"]) == 2
        initial_contexts = {context["focal"]["source_id"]: context
            for stage, context in fake.calls if stage == "NORMALIZACAO"}
        initial_decisions = {
            source_id: {"proposal": item["proposal"], "context": initial_contexts[source_id]}
            for source_id, item in normalizations.items()
        }
        initial, _ = materialize(dataset, initial_decisions, rows)
        assert initial == restored
        joined = join_checkout(restored)
        assert len(joined) == len(dataset.checkout)
        atlas = [row for row in joined if row["ec"] in UNBOUND_ECS]
        assert Counter(row["ec"] for row in atlas) == {
            "1038867654": 86, "1043396662": 75, "1043300981": 73, "1076801732": 57,
        }
        assert all(row["status"] == "PENDENTE" and row["contrato"] is None and row["cnpj"] is None
            and row["taxa_contratada_fracao"] is None for row in atlas)
        assert {row["erro"] for row in atlas} == {"Encontrados 0 contratos aplicáveis; esperado um."}
        assert {row["ec"] for row in joined if row["status"] != "OK"} == UNBOUND_ECS
        assert report["summary"] == {
            "contract_rows": len(current), "canonical_rows": len(canonical),
            "checkout_rows": len(joined), "joined": len(joined) - len(atlas),
            "unresolved_rows": len(atlas), "rca_cases": 4, "human_cases": 4,
        }
        # 1943 linhas do 01 + 14 do 06 (ajustes tardios) = 1957; 291 sem contrato.
        assert len(joined) == 1957
        assert report["summary"]["joined"] == 1666 and report["summary"]["unresolved_rows"] == 291

        assert {path.name for path in output.iterdir()} == {
            "referencias.json", "rca.json", "escaladas_humanas.json",
            "contratos_normalizados.csv", "join_checkout.csv", "fluxo.log",
        }
        persisted = json.loads((output / "referencias.json").read_text(encoding="utf-8"))
        assert persisted == json.loads(json_text(report))
        for decision in persisted["decisions"].values():
            ContractProposal.model_validate(decision["proposal"])
        investigations = json.loads((output / "rca.json").read_text(encoding="utf-8"))
        assert [item["ec"] for item in investigations] == sorted(UNBOUND_ECS)
        for item in investigations:
            Investigation.model_validate(item["result"])
            assert item["rejected"] == [] and item["applied_source_ids"] == []
            assert not item["resolved"] and item["result"]["proposals"] == []
            assert item["context"]["source_scope"] == SOURCE_SCOPE
            assert set(item["context"]["searched_documents"]) == SOURCE_FILES
        horizonte_case = next(item for item in investigations if item["ec"] == "1043396662")
        assert horizonte_id in {row["source_id"] for row in horizonte_case["context"]["related_contracts"]}
        atlas_case = next(item for item in investigations if item["ec"] == "1076801732")
        assert all(row["historical"] or row["establishment_code"] not in {None, "1076801732"}
            for row in atlas_case["context"]["related_contracts"] if "atlas" in row["merchant"].casefold())
        escalations = json.loads((output / "escaladas_humanas.json").read_text(encoding="utf-8"))
        assert [item["ec"] for item in escalations] == sorted(UNBOUND_ECS)
        for escalation in escalations:
            assert escalation["status"] == "ESCALAR_HUMANO" and escalation["quem_decide"] == "Comercial / Financeiro"
            assert set(escalation["operacoes"]) == {row["ref_origem"] for row in atlas if row["ec"] == escalation["ec"]}
            assert escalation["informacao_faltante"].endswith(f"EC {escalation['ec']}.")
            assert set(escalation["fontes_pesquisadas"]) == SOURCE_FILES
            assert escalation["restante_join_processado"] and escalation["impacto_comissao_brl"] is None

        with (output / "contratos_normalizados.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter=";")
            assert reader.fieldnames == [
                "source_id", "cnpj", "establishment_code", "merchant", "modalidade", "taxa_fracao",
                "validity_start", "validity_end", "status", "source_ids", "evidence_ids",
            ]
            exported = list(reader)
        assert len(exported) == sum(len(contract["rates"]) + 1 for contract in canonical)
        assert {row["source_id"] for row in exported} == {contract_id(contract) for contract in restored.contracts}
        assert all("ANTIGO" not in row["source_id"] for row in exported)
        for row in exported:
            original = originals[row["source_id"]]
            expected = original.remittance_rate if row["modalidade"] == "REMESSA" else original.rates[row["modalidade"]]
            assert (Decimal(row["taxa_fracao"]) if row["taxa_fracao"] else None) == expected
            assert row["source_id"] in json.loads(row["source_ids"])
            assert row["source_id"] in json.loads(row["evidence_ids"])
        with (output / "join_checkout.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter=";")
            assert reader.fieldnames == list(joined[0])
            exported_join = list(reader)
        assert len(exported_join) == len(dataset.checkout)
        assert {row["ref_origem"] for row in exported_join} == {entry.source.ref for entry in dataset.checkout}
        assert Counter(row["status"] for row in exported_join) == {"OK": len(joined) - len(atlas), "PENDENTE": len(atlas)}
        assert exported_join == [{key: "" if value is None else str(value) for key, value in row.items()}
            for row in joined]

        period = date(2026, 9, 1)
        baseline, actual = close_month(dataset, period), close_month(restored, period)
        assert actual.competencia == baseline.competencia == "2026-09"
        assert actual.status == baseline.status
        for field, amount in (
            ("comissoes_por_lojista", "comissao_brl"), ("divergencias_taxa", "diferenca_brl"),
            ("notas_a_emitir", "valor_brl"), ("lancamentos_classificados", "valor"), ("pendencias", "valor_brl"),
        ):
            expected_rows, actual_rows = getattr(baseline, field), getattr(actual, field)
            assert {row.id_saida for row in actual_rows} == {row.id_saida for row in expected_rows}, field
            assert sum((getattr(row, amount) or Decimal("0") for row in actual_rows), Decimal("0")) == sum(
                (getattr(row, amount) or Decimal("0") for row in expected_rows), Decimal("0"),
            ), field
            assert {row.id_saida: row for row in actual_rows} == {row.id_saida: row for row in expected_rows}, field
        assert Counter(actual.rastro) == Counter(baseline.rastro)

        changed_manifest = {key: value for key, value in report["input_hashes"].items() if key.startswith("dados/")}
        changed_manifest[next(iter(changed_manifest))] = "0" * 64
        with patch("src.application.normalization.input_manifest", return_value=changed_manifest):
            _expect_error(ValueError, "hashes das fontes divergentes",
                lambda: load_reference(dataset, data_dir, output / "referencias.json"))

        # Referências antigas (v1, sem escopo ou com escopo de docs) não podem ser reaproveitadas.
        legacy_dir = Path(directory) / "legado"
        legacy_dir.mkdir()
        without_scope = {key: value for key, value in persisted.items() if key != "source_scope"}
        for name, legacy in (
            ("v1.json", {**persisted, "version": 1}),
            ("sem-escopo.json", without_scope),
            ("docs.json", {**persisted, "source_scope": "dados_tarvo_e_docs_v1"}),
            ("v1-sem-escopo.json", {**without_scope, "version": 1}),
        ):
            path = legacy_dir / name
            path.write_text(json_text(legacy), encoding="utf-8")
            _expect_error(ValueError, "Referência anterior ou fora do escopo",
                lambda path=path: load_reference(dataset, data_dir, path))
        some_id = next(iter(persisted["decisions"]))
        saved_context = persisted["decisions"][some_id]["context"]
        external_doc = {"source_id": "docs/THREADS_FINANCEIRO_TARVO.md:L1-L12",
            "file": "THREADS_FINANCEIRO_TARVO.md", "text": "vínculo informado em conversa"}
        for broken_context, extra_evidence, message in (
            ({key: value for key, value in saved_context.items() if key != "source_scope"}, None,
                "Contexto de referência fora do escopo"),
            ({**saved_context, "source_scope": "dados_tarvo_e_docs_v1"}, None, "Contexto de referência fora do escopo"),
            ({**saved_context, "related_contracts": []}, None, "não corresponde às evidências atuais"),
            ({**saved_context, "orbe_groups": saved_context["orbe_groups"][:0]}, None, "não corresponde às evidências atuais"),
            ({**saved_context, "documents": [external_doc]}, external_doc["source_id"], "cita evidência inexistente"),
        ):
            tampered = json.loads(json_text(persisted))
            tampered["decisions"][some_id]["context"] = broken_context
            if extra_evidence:
                tampered["decisions"][some_id]["proposal"]["evidence_ids"].append(extra_evidence)
            path = legacy_dir / "contexto.json"
            path.write_text(json_text(tampered), encoding="utf-8")
            _expect_error(ValueError, message, lambda path=path: load_reference(dataset, data_dir, path))
        # Chaves extras no contexto salvo são descartadas: o replay reconstrói o contexto a partir das fontes.
        tampered = json.loads(json_text(persisted))
        tampered["decisions"][some_id]["context"]["documents"] = [external_doc]
        path = legacy_dir / "chave-extra.json"
        path.write_text(json_text(tampered), encoding="utf-8")
        assert load_reference(dataset, data_dir, path) == restored
        injected = json.loads(json_text(persisted))
        # Vínculo sem evidência no contexto (o antigo binding 1043300981→Novo Horizonte aplicado à Horizonte).
        injected["decisions"][horizonte_id]["proposal"]["establishment_code"] = "1043300981"
        path = legacy_dir / "vinculo-externo.json"
        path.write_text(json_text(injected), encoding="utf-8")
        _expect_error(ValueError, "EC inferido não foi encontrado",
            lambda: load_reference(dataset, data_dir, path))
        _expect_error(DataPackError, "Fonte de dados não permitida",
            lambda: load_reference(dataset, ROOT / "docs", output / "referencias.json"))

        cli_output = Path(directory) / "cli"
        cli_fake = _ReferenceInterpreter()
        stdout = StringIO()
        env_file = Path(directory) / "nao-deve-ser-lido.env"
        with patch("src.normalizar.OpenAIInterpreter", return_value=cli_fake) as factory, patch("sys.argv", [
            "normalizar", "--entrada", str(data_dir), "--saida", str(cli_output),
            "--env-file", str(env_file), "--limite-amostras", "2",
        ]), redirect_stdout(stdout):
            normalize_main()
        factory.assert_called_once_with(env_file, cli_output / "cache_llm", log=ANY)
        assert json.loads(stdout.getvalue()) == report["summary"]
        assert {path.name for path in cli_output.iterdir()} == {path.name for path in output.iterdir()}
        assert load_reference(dataset, data_dir, cli_output / "referencias.json") == restored
        with patch("src.normalizar.OpenAIInterpreter", side_effect=AssertionError("não deve instanciar")):
            code, message = _run_cli(["--entrada", str(data_dir), "--docs", str(ROOT / "docs"),
                "--saida", str(Path(directory) / "cli-docs")])
            assert code == 2 and "--docs" in message
            assert not (Path(directory) / "cli-docs").exists()
        with patch("src.normalizar.OpenAIInterpreter", return_value=_ReferenceInterpreter()):
            code, message = _run_cli(["--entrada", str(ROOT / "docs"), "--saida", str(Path(directory) / "cli-fora")])
            assert code == 2 and "Fonte de dados não permitida" in message
            assert not (Path(directory) / "cli-fora" / "referencias.json").exists()

        # Fake que só propõe EC quando o nome Orbe do EC investigado está no próprio contexto.
        evidence_fake = _EvidenceInterpreter()
        inferred_output = Path(directory) / "inferido"
        inferred = normalize_reference(dataset, data_dir, inferred_output, evidence_fake, samples=2)
        investigations = {item["ec"]: item for item in json.loads((inferred_output / "rca.json").read_text(encoding="utf-8"))}
        expected_links = {
            "1038867654": "02_tabela_taxas_contratadas.xlsx!Taxas vigentes!A9:L9",
            "1043300981": "02_tabela_taxas_contratadas.xlsx!Taxas vigentes!A28:L28",
            "1043396662": horizonte_id,
        }
        assert set(investigations) == UNBOUND_ECS
        for ec, source_id in expected_links.items():
            assert investigations[ec]["applied_source_ids"] == [source_id] and investigations[ec]["resolved"]
            assert investigations[ec]["rejected"] == []
            assert inferred["decisions"][source_id]["proposal"]["establishment_code"] == ec
            assert inferred["decisions"][source_id]["context"]["focal"]["source_id"] == f"orbe:EC:{ec}"
        assert not investigations["1076801732"]["resolved"] and investigations["1076801732"]["applied_source_ids"] == []
        assert inferred["summary"] == {**report["summary"], "joined": 1900, "unresolved_rows": 57, "human_cases": 1}
        inferred_escalations = json.loads((inferred_output / "escaladas_humanas.json").read_text(encoding="utf-8"))
        assert [item["ec"] for item in inferred_escalations] == ["1076801732"]
        with (inferred_output / "contratos_normalizados.csv").open(encoding="utf-8", newline="") as stream:
            statuses = {row["source_id"]: (row["status"], row["establishment_code"]) for row in csv.DictReader(stream, delimiter=";")}
        for ec, source_id in expected_links.items():
            assert statuses[source_id] == ("INFERIDO", ec)
        inferred_dataset = load_reference(dataset, data_dir, inferred_output / "referencias.json")
        inferred_close = close_month(inferred_dataset, period)
        inferred_blocked = Counter(row.tipo for row in inferred_close.pendencias)["CHECKOUT_BLOQUEADO"]
        assert inferred_blocked == 57
        new_invoices = {row.tomador_cnpj for row in inferred_close.notas_a_emitir} - {
            row.tomador_cnpj for row in baseline.notas_a_emitir}
        assert new_invoices == {"07752116000126", "66564512000199", "78576234000179"}
        assert "00520815000100" not in {row.tomador_cnpj for row in inferred_close.notas_a_emitir}
        tampered = json.loads((inferred_output / "referencias.json").read_text(encoding="utf-8"))
        tampered["decisions"][expected_links["1043396662"]]["context"]["orbe_groups"] = [
            group for group in tampered["decisions"][expected_links["1043396662"]]["context"]["orbe_groups"]
            if group["establishment_code"] != "1043396662"
        ]
        path = inferred_output / "adulterado.json"
        path.write_text(json_text(tampered), encoding="utf-8")
        _expect_error(ValueError, "não corresponde às evidências atuais",
            lambda: load_reference(dataset, data_dir, path))


def _check_email_evidence() -> None:
    data_dir = Path(__file__).resolve().parents[2] / "dados_tarvo"
    dataset = load_dataset(data_dir)
    rows = contract_rows(data_dir, dataset)
    chunks = email_chunks(data_dir)
    assert chunks
    assert any(c["source_id"].endswith(":headers") and "relatorios@pontecambio.com.br" in c["text"] for c in chunks)
    assert any("50%" in c["text"] and "spread" in c["text"] for c in chunks)
    assert any("75.374.179/0001-46" in c["text"] for c in chunks)

    kiwi = next(r for r in rows if not r["historical"] and r["cnpj"] == "07752116000126")
    ctx = context_for(kiwi, rows, dataset, 75, 2, 8)
    assert ctx["partner_email"]["partner_cnpj"] == "75374179000146"
    assert any(g["cnpj"] == "07752116000126" and "KIWI EXPERIENCE TURI" in g["names"] for g in ctx["remittance_groups"])
    assert ctx["bank_mentions"]
    remit_sample = ctx["remittance_groups"][0]["samples"][0]["source_id"]
    assert "spread_rate" not in ctx["remittance_groups"][0]["samples"][0]
    proposal = _proposal_for_row(kiwi, evidence=(remit_sample, ctx["partner_email"]["source_id"], ctx["bank_mentions"][0]["source_id"]))
    validate_proposal(proposal, ctx, rows)

def check() -> None:
    _check_schemas()
    _check_candidates()
    _check_validity()
    _check_real_contracts()
    _check_source_scope()
    _check_interpreter()
    _check_proposal_validation()
    _check_email_evidence()
    _check_reference_pipeline()


if __name__ == "__main__":
    check()
