from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ContractProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    establishment_code: str | None
    validity_start: str | None
    validity_end: str | None
    disposition: Literal["UTILIZAVEL", "PENDENTE", "INATIVO"]
    evidence_ids: list[str]
    reason: str = Field(min_length=1)


class Investigation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    diagnosis: str = Field(min_length=1)
    hypotheses: list[str]
    evidence_ids: list[str]
    proposals: list[ContractProposal]
    missing_information: str
