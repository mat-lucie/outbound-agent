"""PR-38 tests — ``create_deal_from_response`` threshold gates,
idempotency contract, escalation payload, and ``AttioClient.create_deal``
wire-format."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from workflows.deal_creation import (
    DEAL_AUTO_THRESHOLD,
    DEAL_CONFIRM_THRESHOLD,
    DEAL_STAGE_LEAD,
    WRITER_MODULE,
    DealCreationResult,
    _build_deal_attributes,
    _find_existing_deal,
    build_idempotency_key,
    create_deal_from_response,
)

# ────────────────────────────────────────────────────────────────────────
# Idempotency key
# ────────────────────────────────────────────────────────────────────────


class TestBuildIdempotencyKey:
    def test_well_formed_triple(self):
        ts = datetime(2026, 5, 10, 14, 30, 0, tzinfo=UTC)
        key = build_idempotency_key(
            person_record_id="p-1",
            response_classification="positive",
            response_received_at=ts,
        )
        assert key == "p-1_positive_2026-05-10T14:30:00+00:00"

    def test_different_timestamps_distinct_keys(self):
        a = build_idempotency_key(
            person_record_id="p-1", response_classification="positive",
            response_received_at=datetime(2026, 5, 10, tzinfo=UTC),
        )
        b = build_idempotency_key(
            person_record_id="p-1", response_classification="positive",
            response_received_at=datetime(2026, 5, 11, tzinfo=UTC),
        )
        assert a != b

    def test_different_classifications_distinct_keys(self):
        ts = datetime(2026, 5, 10, tzinfo=UTC)
        a = build_idempotency_key(
            person_record_id="p-1", response_classification="positive",
            response_received_at=ts,
        )
        b = build_idempotency_key(
            person_record_id="p-1", response_classification="interested",
            response_received_at=ts,
        )
        assert a != b

    def test_blank_person_id_raises(self):
        with pytest.raises(ValueError, match="person_record_id"):
            build_idempotency_key(
                person_record_id="",
                response_classification="positive",
                response_received_at=datetime(2026, 5, 10, tzinfo=UTC),
            )

    def test_blank_classification_raises(self):
        with pytest.raises(ValueError, match="response_classification"):
            build_idempotency_key(
                person_record_id="p-1",
                response_classification="",
                response_received_at=datetime(2026, 5, 10, tzinfo=UTC),
            )

    def test_naive_datetime_raises_tz_aware_required(self):
        """Naive datetimes break idempotency on cron replay — reject at
        the boundary so the failure is loud."""
        with pytest.raises(ValueError, match="timezone-aware"):
            build_idempotency_key(
                person_record_id="p-1",
                response_classification="positive",
                response_received_at=datetime(2026, 5, 10, 14, 30, 0),  # naive
            )


# ────────────────────────────────────────────────────────────────────────
# AttioClient.create_deal wire format
# ────────────────────────────────────────────────────────────────────────


class TestAttioClientCreateDeal:
    def test_posts_to_deals_records(self):
        from clients.attio import AttioClient

        attio = AttioClient.__new__(AttioClient)
        attio._request = MagicMock(return_value={
            "data": {"id": {"record_id": "d-1"}, "values": {"name": [{"value": "x"}]}}
        })

        out = attio.create_deal({"name": "x", "stage": "Lead"})
        attio._request.assert_called_once()
        args, kwargs = attio._request.call_args
        assert args[0] == "POST"
        assert args[1] == "/objects/deals/records"
        # Body wraps `values` under `data` per the existing create_company shape.
        assert kwargs["json"] == {"data": {"values": {"name": "x", "stage": "Lead"}}}
        assert out["id"]["record_id"] == "d-1"


# ────────────────────────────────────────────────────────────────────────
# _build_deal_attributes
# ────────────────────────────────────────────────────────────────────────


class TestBuildDealAttributes:
    def test_minimum_required_attributes(self):
        attrs = _build_deal_attributes(
            idempotency_key="k1",
            deal_name="Acme – Daisy",
            person_record_id="p-1",
            associated_company_id=None,
            created_via="agent_auto",
        )
        assert attrs["name"] == "Acme – Daisy"
        assert attrs["stage"] == DEAL_STAGE_LEAD
        assert attrs["creation_idempotency_key"] == "k1"
        assert attrs["created_via"] == "agent_auto"
        assert attrs["associated_people"] == [
            {"target_object": "people", "target_record_id": "p-1"},
        ]
        # No company link when associated_company_id is None.
        assert "associated_company" not in attrs

    def test_includes_company_when_provided(self):
        attrs = _build_deal_attributes(
            idempotency_key="k1",
            deal_name="Acme – Daisy",
            person_record_id="p-1",
            associated_company_id="c-1",
            created_via="agent_confirm",
        )
        assert attrs["associated_company"] == [
            {"target_object": "companies", "target_record_id": "c-1"},
        ]
        assert attrs["created_via"] == "agent_confirm"


# ────────────────────────────────────────────────────────────────────────
# Find-existing-deal idempotency lookup
# ────────────────────────────────────────────────────────────────────────


class TestFindExistingDeal:
    def test_no_match_returns_none(self):
        attio = MagicMock()
        attio._request.return_value = {"data": []}
        assert _find_existing_deal(attio, idempotency_key="k1") is None

    def test_match_returns_first_row(self):
        attio = MagicMock()
        attio._request.return_value = {
            "data": [{"id": {"record_id": "d-1"}}, {"id": {"record_id": "d-2"}}]
        }
        out = _find_existing_deal(attio, idempotency_key="k1")
        assert out["id"]["record_id"] == "d-1"

    def test_filter_uses_eq_form_on_creation_idempotency_key(self):
        attio = MagicMock()
        attio._request.return_value = {"data": []}
        _find_existing_deal(attio, idempotency_key="k-special")
        _, kwargs = attio._request.call_args
        # Explicit $eq form mirroring workflows/escalation.py:302; bare-value
        # also works in this codebase (PR-34/PR-35 precedent inside $and)
        # but explicit is more defensive against API-shape drift.
        assert kwargs["json"]["filter"] == {
            "creation_idempotency_key": {"$eq": "k-special"},
        }
        assert kwargs["json"]["limit"] == 1


# ────────────────────────────────────────────────────────────────────────
# create_deal_from_response — threshold gates
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def attio_no_existing():
    """Attio mock: idempotency lookup returns no row; create_deal returns d-new."""
    attio = MagicMock()
    attio._request.return_value = {"data": []}
    attio.create_deal.return_value = {"id": {"record_id": "d-new"}}
    return attio


class TestCreateDealFromResponse:
    ts = datetime(2026, 5, 10, 14, 30, 0, tzinfo=UTC)

    def test_auto_create_above_threshold(self, attio_no_existing):
        result = create_deal_from_response(
            person_record_id="p-1",
            response_classification="positive",
            response_received_at=self.ts,
            classifier_confidence=0.95,
            deal_name="Acme – Daisy",
            associated_company_id="c-1",
            attio=attio_no_existing,
        )
        assert result.action == "created"
        assert result.deal_record_id == "d-new"
        assert result.created_via == "agent_auto"
        assert result.idempotency_key == "p-1_positive_2026-05-10T14:30:00+00:00"
        attio_no_existing.create_deal.assert_called_once()

    def test_confirm_band_escalates_no_create(self, attio_no_existing):
        with patch("workflows.deal_creation.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-1"}}
            result = create_deal_from_response(
                person_record_id="p-1",
                response_classification="positive",
                response_received_at=self.ts,
                classifier_confidence=0.70,
                deal_name="Acme – Daisy",
                associated_company_id="c-1",
                attio=attio_no_existing,
            )
        assert result.action == "escalated_confirm"
        assert result.deal_record_id is None
        assert result.escalation_record_id == "row-1"
        # IMPORTANT: no deal created on the confirm path
        attio_no_existing.create_deal.assert_not_called()
        # Escalation kwargs check
        kwargs = m_escalate.call_args.kwargs
        assert kwargs["type"] == "deal_creation_confirm"
        assert kwargs["idempotency_key"] == result.idempotency_key
        payload = kwargs["payload"]
        assert payload["classifier_confidence"] == 0.70
        assert payload["candidate_deal"]["creation_idempotency_key"] == result.idempotency_key
        assert payload["candidate_deal"]["stage"] == DEAL_STAGE_LEAD
        assert payload["opened_by"] == WRITER_MODULE

    def test_below_confirm_threshold_skips_silently(self, attio_no_existing):
        with patch("workflows.deal_creation.escalate") as m_escalate:
            result = create_deal_from_response(
                person_record_id="p-1",
                response_classification="positive",
                response_received_at=self.ts,
                classifier_confidence=0.40,
                deal_name="Acme – Daisy",
                associated_company_id=None,
                attio=attio_no_existing,
            )
        assert result.action == "skipped_low_confidence"
        assert result.deal_record_id is None
        attio_no_existing.create_deal.assert_not_called()
        m_escalate.assert_not_called()

    def test_idempotent_hit_returns_cached(self):
        attio = MagicMock()
        # Existing deal at the idempotency key.
        attio._request.return_value = {
            "data": [{"id": {"record_id": "d-existing"}}],
        }
        with patch("workflows.deal_creation.escalate") as m_escalate:
            result = create_deal_from_response(
                person_record_id="p-1",
                response_classification="positive",
                response_received_at=self.ts,
                classifier_confidence=0.99,  # would auto-create if not cached
                deal_name="Acme – Daisy",
                associated_company_id="c-1",
                attio=attio,
            )
        assert result.action == "cached_idempotent"
        assert result.deal_record_id == "d-existing"
        attio.create_deal.assert_not_called()
        m_escalate.assert_not_called()


class TestThresholdBoundaries:
    """Mutation-survival boundary tests around the two thresholds."""

    ts = datetime(2026, 5, 10, tzinfo=UTC)

    def test_confidence_exactly_auto_creates_inclusive_ge(self, attio_no_existing):
        # Inclusive >= on auto threshold — exactly DEAL_AUTO_THRESHOLD creates.
        # Requires a company link; without one the orphan-deal gate forces
        # the confirm path even at auto-threshold.
        result = create_deal_from_response(
            person_record_id="p-1",
            response_classification="positive",
            response_received_at=self.ts,
            classifier_confidence=DEAL_AUTO_THRESHOLD,
            deal_name="X",
            associated_company_id="c-1",
            attio=attio_no_existing,
        )
        assert result.action == "created"

    def test_confidence_exactly_confirm_escalates_inclusive_ge(self, attio_no_existing):
        # Inclusive >= on confirm threshold — exactly DEAL_CONFIRM_THRESHOLD escalates.
        with patch("workflows.deal_creation.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "r1"}}
            result = create_deal_from_response(
                person_record_id="p-1",
                response_classification="positive",
                response_received_at=self.ts,
                classifier_confidence=DEAL_CONFIRM_THRESHOLD,
                deal_name="X",
                associated_company_id=None,
                attio=attio_no_existing,
            )
        assert result.action == "escalated_confirm"

    def test_confidence_just_below_confirm_skips_strict_lt(self, attio_no_existing):
        # Strict < on confirm threshold — DEAL_CONFIRM_THRESHOLD - epsilon skips.
        result = create_deal_from_response(
            person_record_id="p-1",
            response_classification="positive",
            response_received_at=self.ts,
            classifier_confidence=DEAL_CONFIRM_THRESHOLD - 0.0001,
            deal_name="X",
            associated_company_id=None,
            attio=attio_no_existing,
        )
        assert result.action == "skipped_low_confidence"

    def test_invalid_threshold_ordering_raises(self, attio_no_existing):
        with pytest.raises(ValueError, match="confirm_threshold"):
            create_deal_from_response(
                person_record_id="p-1",
                response_classification="positive",
                response_received_at=self.ts,
                classifier_confidence=0.9,
                deal_name="X",
                associated_company_id="c-1",
                attio=attio_no_existing,
                auto_threshold=0.5,
                confirm_threshold=0.7,  # > auto → invalid
            )


class TestResultDataclass:
    def test_is_frozen(self):
        r = DealCreationResult(
            action="created", idempotency_key="k", confidence=0.9,
            rationale="r", deal_record_id="d",
        )
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            r.action = "skipped_low_confidence"  # type: ignore[misc]


class TestOrphanDealGate:
    """Even at high confidence, deal creation MUST NOT silently produce
    a deal that has no associated_company. The confirm-path takes the
    fall so the operator sees a typed signal."""

    ts = datetime(2026, 5, 10, tzinfo=UTC)

    def test_high_confidence_without_company_escalates_not_creates(self, attio_no_existing):
        with patch("workflows.deal_creation.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-orphan"}}
            result = create_deal_from_response(
                person_record_id="p-1",
                response_classification="positive",
                response_received_at=self.ts,
                classifier_confidence=0.99,   # well above auto threshold
                deal_name="OrphanCo – Daisy",
                associated_company_id=None,    # KEY: no company link
                attio=attio_no_existing,
            )
        assert result.action == "escalated_confirm"
        assert result.deal_record_id is None
        assert "orphan" in result.rationale.lower()
        attio_no_existing.create_deal.assert_not_called()
        m_escalate.assert_called_once()

    def test_high_confidence_with_company_does_create(self, attio_no_existing):
        # Counterfactual: same confidence but WITH a company → auto-create.
        with patch("workflows.deal_creation.escalate") as m_escalate:
            result = create_deal_from_response(
                person_record_id="p-1",
                response_classification="positive",
                response_received_at=self.ts,
                classifier_confidence=0.99,
                deal_name="Real Co – Daisy",
                associated_company_id="c-1",
                attio=attio_no_existing,
            )
        assert result.action == "created"
        attio_no_existing.create_deal.assert_called_once()
        m_escalate.assert_not_called()
