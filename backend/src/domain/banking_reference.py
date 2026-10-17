from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class BankProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    movement_id: str
    account_code: str | None
    evidence_ids: list[str]
    reason: str = Field(min_length=1)
    missing_information: str


class BankInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    diagnosis: str = Field(min_length=1)
    hypotheses: list[str]
    evidence_ids: list[str]
    proposal: BankProposal | None
    missing_information: str


@dataclass(frozen=True, slots=True)
class ReviewedBankDecision:
    movement_id: str
    account_code: str
    reason: str
    evidence_ids: tuple[str, ...]
    reviewed_by: str
    reference_id: str
