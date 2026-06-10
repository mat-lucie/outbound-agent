"""Prospect data model."""

import json
from dataclasses import dataclass, field
from datetime import date

from .campaign import Language, Persona
from .pipeline import PipelineStage


@dataclass
class Prospect:
    """A prospect in the LinkedIn outreach pipeline."""
    name: str
    company: str
    title: str
    linkedin_url: str
    language: Language
    persona: Persona
    stage: PipelineStage = PipelineStage.PROSPECT
    attio_record_id: str | None = None
    attio_entry_id: str | None = None
    quality_score: int = 0
    dm_step: int = 0
    last_contact_date: date | None = None
    location: str = ""
    employee_count: int | None = None
    # Scorer signals — persisted to Attio for deterministic, queryable debugging.
    # All optional so existing callers that don't populate them continue to work.
    score_breakdown: dict | None = field(default=None)
    scoring_lane: str | None = None
    verdict_path: str | None = None
    llm_rationale: str | None = None
    # Auto-research diagnostic signals (Phase 1, 2026-05). Denormalized for
    # queryability — Attio doesn't filter on JSON-in-text, so we surface
    # icp_lane and a discrete score band as native attributes.
    icp_lane_persisted: int | None = None
    quality_score_band: str | None = None
    defensive_score: float | None = None
    engagement_score: float | None = None

    @property
    def passes_quality_gate(self) -> bool:
        """Quick check if score meets minimum threshold."""
        return self.quality_score >= 60

    def to_attio_attributes(self) -> dict:
        """Convert to Attio record attributes for upsert."""
        attrs: dict = {
            "name": [{"first_name": self.name.split()[0], "last_name": " ".join(self.name.split()[1:]) or self.name}],
        }
        if self.linkedin_url:
            attrs["linkedin"] = self.linkedin_url
        return attrs

    def to_attio_entry_attributes(self) -> dict:
        """Convert to Attio list entry attributes.

        NOTE: production write path is `workflows.weekly_prospect.
        _build_prospect_entry_attrs` — that's the function the live commit
        paths call. This method is kept as the canonical model-layer
        equivalent (and is what tests/scripts using the Prospect dataclass
        rely on). When adding a new persisted field, update BOTH this
        method and `_build_prospect_entry_attrs` to keep them in sync.
        """
        entry: dict = {}
        if self.persona:
            entry["persona"] = self.persona.value
        if self.language:
            entry["language"] = self.language.value
        entry["quality_score"] = self.quality_score
        entry["dm_step"] = self.dm_step
        if self.last_contact_date:
            entry["last_contact_date"] = self.last_contact_date.isoformat()
        if self.score_breakdown is not None:
            entry["score_breakdown"] = json.dumps(self.score_breakdown)
        if self.scoring_lane:
            entry["scoring_lane"] = self.scoring_lane
        if self.verdict_path:
            entry["verdict_path"] = self.verdict_path
        if self.llm_rationale:
            entry["llm_rationale"] = self.llm_rationale
        if self.icp_lane_persisted is not None:
            entry["icp_lane_persisted"] = self.icp_lane_persisted
        if self.quality_score_band:
            entry["quality_score_band"] = self.quality_score_band
        if self.defensive_score is not None:
            entry["defensive_score"] = self.defensive_score
        if self.engagement_score is not None:
            entry["engagement_score"] = self.engagement_score
        return entry
