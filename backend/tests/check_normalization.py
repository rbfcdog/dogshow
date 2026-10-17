import csv
import json
import os
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
from src.domain.models import CheckoutEntry, Contract, Dataset, SourceRef
from src.domain.normalization import ContractProposal, Investigation
from src.domain.policies import ESTABLISHMENT_BINDINGS, active_on, contract_candidates
from src.infrastructure.normalization_sources import (
    contract_id,
    contract_rows,
    email_chunks,
    json_text,
    normalize_name,
    orbe_candidates,
    related_rows,
    similarity,
)
from src.infrastructure.openai_normalizer import ModelUnavailable, OpenAIInterpreter, SYSTEM_PROMPT
from src.infrastructure.readers import load_dataset
from src.normalizar import main as normalize_main


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
    assert set(by_ec) == {"EC-EXATO", "1043300981", "EC-NOME"}
    assert by_ec["1043300981"]["confirmed_cnpj"] == focal["cnpj"]
    assert by_ec["1043300981"]["confirmation_source"]
    assert by_ec["EC-NOME"]["confirmed_cnpj"] is None
    assert by_ec["EC-NOME"]["confirmation_source"] is None
    assert by_ec["EC-NOME"]["name_score"] == 100
    for candidate in selected:
        assert candidate["total_rows"] == 3
        assert [sample["source"]["ref"] for sample in candidate["samples"]] == ["T1", "T2"]
        assert {sample["establishment_code"] for sample in candidate["samples"]} == {candidate["establishment_code"]}
    assert orbe_candidates(focal, replace(dataset, checkout=tuple(reversed(entries))), 90, 2) == selected
    assert all(len(candidate["samples"]) == 1 for candidate in orbe_candidates(focal, dataset, 90, 1))
    unconfirmed = {**focal, "cnpj": "99999999000199"}
    assert {candidate["establishment_code"] for candidate in orbe_candidates(unconfirmed, dataset, 90, 2)} == {
        "EC-EXATO", "EC-NOME",
    }


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
                "focal": {"source_id": proposal.source_id}, "candidates": [],
                "sample": {"rate": Decimal("0.0269"), "on": date(2026, 9, 14)},
            }
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

        os.environ.pop("OPENAI_API_KEY")
        with patch("src.infrastructure.openai_normalizer.OpenAI") as factory:
            try:
                OpenAIInterpreter(root / "ausente.env", root / "unused-cache")
            except ModelUnavailable as error:
                assert "OPENAI_API_KEY ausente" in str(error)
            else:
                raise AssertionError("Cliente sem chave foi criado.")
            factory.assert_not_called()


def _proposal_for_row(row: dict, *, pending: bool = False) -> ContractProposal:
    ec = row["establishment_code"] or next(
        (ec for ec, cnpj in ESTABLISHMENT_BINDINGS.items() if cnpj == row["cnpj"]), None,
    )
    disposition = "INATIVO" if "INATIVO" in (row["note"] or "").upper() else (
        "PENDENTE" if pending or not row["validity"] else "UTILIZAVEL"
    )
    start, end = explicit_bounds(row["validity"]) if row["validity"] else (None, None)
    return ContractProposal(
        source_id=row["source_id"], establishment_code=ec,
        validity_start=start.isoformat() if start else None,
        validity_end=end.isoformat() if end else None,
        disposition=disposition, evidence_ids=[row["source_id"]],
        reason="Proposta de teste baseada exclusivamente na linha e nos vínculos confirmados.",
    )


class _ReferenceInterpreter:
    model = "interpretador-fake-sem-api"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, context: dict, stage: str) -> ContractProposal | Investigation:
        self.calls.append((stage, context))
        focal = context["focal"]
        if stage == "NORMALIZACAO":
            assert not focal["historical"]
            proposal = _proposal_for_row(focal, pending=focal["merchant"] == "Horizonte Viagens")
            if focal["source_id"].endswith("!A6:L6"):
                proposal = proposal.model_copy(update={"validity_end": "14/09/2026"})
            return proposal
        assert stage == "RCA"
        if focal["establishment_code"] == "1043396662":
            row = next(row for row in context["related_contracts"]
                if row["cnpj"] == "78576234000179" and not row["historical"])
            proposal = _proposal_for_row(row)
            return Investigation(
                diagnosis="Horizonte Viagens possui linha atual e vínculo confirmado pelo financeiro.",
                hypotheses=[], evidence_ids=proposal.evidence_ids, proposals=[proposal],
                missing_information="",
            )
        assert focal["establishment_code"] == "1076801732"
        historical = [row for row in context["related_contracts"]
            if row["historical"] and "atlas" in row["merchant"].casefold()]
        assert historical
        return Investigation(
            diagnosis="Atlas tem somente evidência histórica de identidade, sem contrato atual.",
            hypotheses=["Contrato vigente ainda não documentado no pacote."],
            evidence_ids=[row["source_id"] for row in historical], proposals=[],
            missing_information="Comercial deve confirmar taxas e vigência atuais da Atlas.",
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


def _check_reference_pipeline() -> None:
    root = Path(__file__).resolve().parents[2]
    data_dir, docs_dir = root / "dados_tarvo", root / "docs"
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
        report = normalize_reference(dataset, data_dir, docs_dir, output, fake, samples=2)
        assert report["version"] == 1 and report["model"] == fake.model
        assert report["settings"] == {"samples": 2, "threshold": 75, "candidate_limit": 8}
        normalizations = {item["source_id"]: item for item in report["normalization"]}
        assert set(normalizations) == set(originals)
        assert all(item["validation_error"] is None for item in normalizations.values())
        assert Counter(stage for stage, _ in fake.calls) == {"NORMALIZACAO": len(current), "RCA": 2}
        assert Counter(context["focal"]["establishment_code"] for stage, context in fake.calls if stage == "RCA") == {
            "1043396662": 1, "1076801732": 1,
        }
        for stage, context in fake.calls:
            assert all(len(group["samples"]) <= 2 for group in context["orbe_groups"])
            if stage == "NORMALIZACAO":
                assert context["selection"]["samples_per_ec"] == 2
                assert any(doc["file"].endswith(".eml") for doc in context.get("documents", []))

        horizonte = next(row for row in current if row["merchant"] == "Horizonte Viagens")
        horizonte_id = horizonte["source_id"]
        assert normalizations[horizonte_id]["proposal"]["disposition"] == "PENDENTE"
        assert report["decisions"][horizonte_id]["proposal"]["disposition"] == "UTILIZAVEL"
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
        initial_join = join_checkout(initial)
        assert {row["status"] for row in initial_join if row["ec"] == "1043396662"} == {"PENDENTE"}
        joined = join_checkout(restored)
        assert len(joined) == len(dataset.checkout)
        assert {row["status"] for row in joined if row["ec"] == "1043396662"} == {"OK"}
        atlas = [row for row in joined if row["ec"] == "1076801732"]
        assert atlas and all(row["status"] == "PENDENTE" and row["contrato"] is None
            and row["taxa_contratada_fracao"] is None for row in atlas)
        assert {row["ec"] for row in joined if row["status"] != "OK"} == {"1076801732"}
        assert report["summary"] == {
            "contract_rows": len(current), "canonical_rows": len(canonical),
            "checkout_rows": len(joined), "joined": len(joined) - len(atlas),
            "unresolved_rows": len(atlas), "rca_cases": 2, "human_cases": 1,
        }

        assert {path.name for path in output.iterdir()} == {
            "referencias.json", "rca.json", "escaladas_humanas.json",
            "contratos_normalizados.csv", "join_checkout.csv",
        }
        persisted = json.loads((output / "referencias.json").read_text(encoding="utf-8"))
        assert persisted == json.loads(json_text(report))
        for decision in persisted["decisions"].values():
            ContractProposal.model_validate(decision["proposal"])
        investigations = json.loads((output / "rca.json").read_text(encoding="utf-8"))
        assert {item["ec"] for item in investigations} == {"1043396662", "1076801732"}
        for item in investigations:
            Investigation.model_validate(item["result"])
            assert item["rejected"] == []
        resolved = next(item for item in investigations if item["ec"] == "1043396662")
        assert resolved["resolved"] and resolved["applied_source_ids"] == [horizonte_id]
        assert {proposal["source_id"] for proposal in resolved["result"]["proposals"]} == {horizonte_id}
        assert not next(row for row in resolved["context"]["related_contracts"]
            if row["source_id"] == horizonte_id)["historical"]
        residual = next(item for item in investigations if item["ec"] == "1076801732")
        assert not residual["resolved"] and residual["applied_source_ids"] == []
        assert residual["result"]["proposals"] == []
        escalations = json.loads((output / "escaladas_humanas.json").read_text(encoding="utf-8"))
        assert len(escalations) == 1
        escalation = escalations[0]
        assert escalation["ec"] == "1076801732" and escalation["status"] == "ESCALAR_HUMANO"
        assert set(escalation["operacoes"]) == {row["ref_origem"] for row in atlas}
        assert escalation["informacao_faltante"] and escalation["fontes_pesquisadas"]
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
            try:
                load_reference(dataset, data_dir, output / "referencias.json")
            except ValueError as error:
                assert "hashes das fontes divergentes" in str(error)
            else:
                raise AssertionError("Replay aceitou fontes alteradas.")

        cli_output = Path(directory) / "cli"
        cli_fake = _ReferenceInterpreter()
        stdout = StringIO()
        env_file = Path(directory) / "nao-deve-ser-lido.env"
        with patch("src.normalizar.OpenAIInterpreter", return_value=cli_fake) as factory, patch("sys.argv", [
            "normalizar", "--entrada", str(data_dir), "--docs", str(docs_dir), "--saida", str(cli_output),
            "--env-file", str(env_file), "--limite-amostras", "2",
        ]), redirect_stdout(stdout):
            normalize_main()
        factory.assert_called_once_with(env_file, cli_output / "cache_llm")
        assert json.loads(stdout.getvalue()) == report["summary"]
        assert {path.name for path in cli_output.iterdir()} == {path.name for path in output.iterdir()}
        assert load_reference(dataset, data_dir, cli_output / "referencias.json") == restored


def _check_email_evidence() -> None:
    data_dir = Path(__file__).resolve().parents[2] / "dados_tarvo"
    chunks = email_chunks(data_dir)
    assert chunks
    assert any(c["source_id"].endswith(":headers") and "relatorios@pontecambio.com.br" in c["text"] for c in chunks)
    assert any("50%" in c["text"] and "spread" in c["text"] for c in chunks)
    assert any("75.374.179/0001-46" in c["text"] for c in chunks)


def check() -> None:
    _check_schemas()
    _check_candidates()
    _check_validity()
    _check_real_contracts()
    _check_interpreter()
    _check_proposal_validation()
    _check_email_evidence()
    _check_reference_pipeline()


if __name__ == "__main__":
    check()
