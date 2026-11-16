"""Government as an active market participant, and the policy pipeline.

Headlines collapse "proposal" into "outcome". This module keeps every stage separate
and estimates the probability and timing of reaching company economics from the
stage a policy has *actually* reached (evidenced by Tier-1 records).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum, IntEnum


class GovRole(str, Enum):
    CUSTOMER = "customer"
    FINANCIER = "financier"
    INSURER = "insurer"
    COMPETITOR = "competitor"
    GATEKEEPER = "gatekeeper"
    PRICE_SETTER = "price_setter"
    LANDLORD = "landlord"
    STANDARD_SETTER = "standard_setter"
    SUPPLY_ALLOCATOR = "supply_allocator"
    SANCTIONS_AUTHORITY = "sanctions_authority"
    TECHNOLOGY_SPONSOR = "technology_sponsor"


class PolicyStage(IntEnum):
    PROPOSAL = 0
    BILL = 1
    COMMITTEE = 2
    APPROPRIATION = 3
    AGENCY_RULE = 4       # proposed rule / NPRM
    PUBLIC_COMMENT = 5
    FINAL_RULE = 6
    ENFORCEMENT = 7
    ECONOMIC_EFFECT = 8


# Illustrative base rates: P(advance to next stage) and median days spent in the stage.
# These are placeholders to be replaced by measured rates from the historical record
# (Congress.gov / Federal Register / USAspending) once ingestion is live.
STAGE_PASS_RATE = {
    PolicyStage.PROPOSAL: 0.35, PolicyStage.BILL: 0.30, PolicyStage.COMMITTEE: 0.55,
    PolicyStage.APPROPRIATION: 0.80, PolicyStage.AGENCY_RULE: 0.85, PolicyStage.PUBLIC_COMMENT: 0.90,
    PolicyStage.FINAL_RULE: 0.95, PolicyStage.ENFORCEMENT: 0.95,
}
STAGE_MEDIAN_DAYS = {
    PolicyStage.PROPOSAL: 90, PolicyStage.BILL: 120, PolicyStage.COMMITTEE: 120,
    PolicyStage.APPROPRIATION: 150, PolicyStage.AGENCY_RULE: 120, PolicyStage.PUBLIC_COMMENT: 75,
    PolicyStage.FINAL_RULE: 60, PolicyStage.ENFORCEMENT: 90,
}

# Executive actions (export controls, sanctions, procurement awards) skip the legislative stages.
EXECUTIVE_PATH = [PolicyStage.AGENCY_RULE, PolicyStage.FINAL_RULE, PolicyStage.ENFORCEMENT,
                  PolicyStage.ECONOMIC_EFFECT]
LEGISLATIVE_PATH = list(PolicyStage)


@dataclass
class PolicyItem:
    id: str
    name: str
    kind: str                         # subsidy | tariff | export_control | procurement | regulation | tax_credit
    stage: PolicyStage
    path: str = "legislative"         # legislative | executive
    agency: str | None = None         # implementing agency entity id
    committees: list[str] = field(default_factory=list)
    statute: str | None = None
    appropriated: bool | None = None  # None = not applicable
    stage_entered: date | None = None
    evidence_ids: list[str] = field(default_factory=list)
    affects: dict[str, float] = field(default_factory=dict)   # entity id -> signed effect if enacted
    stage_history: list[tuple[str, str]] = field(default_factory=list)

    def remaining_stages(self) -> list[PolicyStage]:
        seq = EXECUTIVE_PATH if self.path == "executive" else LEGISLATIVE_PATH
        return [s for s in seq if s >= self.stage and s != PolicyStage.ECONOMIC_EFFECT]

    def p_effect(self) -> float:
        """Probability this item reaches company economics from where it actually is."""
        if self.stage >= PolicyStage.ECONOMIC_EFFECT:
            return 1.0
        p = math.prod(STAGE_PASS_RATE[s] for s in self.remaining_stages())
        if self.kind in ("subsidy", "procurement") and self.appropriated is False \
                and self.stage > PolicyStage.APPROPRIATION:
            p *= 0.4  # authorized but unfunded: a common failure mode
        return p

    def expected_effect_date(self, as_of: date) -> date:
        days = sum(STAGE_MEDIAN_DAYS[s] for s in self.remaining_stages())
        if self.stage_entered and self.remaining_stages():
            days -= min((as_of - self.stage_entered).days, STAGE_MEDIAN_DAYS[self.stage] - 1)
        return as_of + timedelta(days=max(days, 0))

    def gaps(self) -> list[str]:
        """Prerequisites that headlines typically skip over."""
        out = []
        if self.kind in ("subsidy", "procurement", "tax_credit") and self.path == "legislative":
            if self.appropriated is not True:
                out.append("implementing agency has not received appropriations")
        if self.stage < PolicyStage.FINAL_RULE:
            out.append(f"no final rule yet (currently at {self.stage.name.lower()})")
        return out

    def to_dict(self, as_of: date) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "path": self.path,
            "stage": self.stage.name, "agency": self.agency, "committees": self.committees,
            "statute": self.statute, "appropriated": self.appropriated,
            "p_economic_effect": round(self.p_effect(), 3),
            "expected_effect_date": self.expected_effect_date(as_of).isoformat(),
            "gaps": self.gaps(), "affects": self.affects, "evidence_ids": self.evidence_ids,
        }
