"""Tests for workflows.cross_channel_suppression — PR-37 sole writer of
``suppress_re_engagement``. Belt-and-suspenders OR over three independent
suppression signals."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from workflows.cross_channel_suppression import (
    EMAIL_HARD_DECLINE_STAGES,
    LINKEDIN_OUTREACH_LIST_ID_ENV,
    NEGATIVE_RESPONSE_CLASSIFICATIONS,
    SUPPRESSED_STAGES,
    EmailLaneNotProvisioned,
    apply_suppression,
    build_suppression_set,
    email_campaign_stage_provisioned,
    email_hard_decline_ids,
    email_person_ids_in_stages,
    is_suppressed,
)


def _entry(
    *,
    record_id: str = "rec-1",
    entry_id: str = "ent-1",
    stage: str | None = None,
    response_classification: str | None = None,
    suppress_re_engagement: bool | None = None,
) -> dict:
    """Build a parsed-entry dict (the shape AttioClient.parse_entry returns)."""
    return {
        "entry_id": entry_id,
        "record_id": record_id,
        "stage": stage,
        "response_classification": response_classification,
        "suppress_re_engagement": suppress_re_engagement,
    }


class TestIsSuppressed:
    def test_suppress_flag_true(self):
        assert is_suppressed(_entry(suppress_re_engagement=True)) is True

    def test_response_negative(self):
        assert is_suppressed(_entry(response_classification="negative")) is True

    def test_response_defensive(self):
        assert is_suppressed(_entry(response_classification="defensive")) is True

    def test_stage_not_interested(self):
        assert is_suppressed(_entry(stage="Not Interested")) is True

    def test_stage_defensive_hold(self):
        assert is_suppressed(_entry(stage="Defensive Hold")) is True

    def test_clean_row(self):
        assert is_suppressed(_entry()) is False

    def test_positive_response_not_suppressed(self):
        assert is_suppressed(_entry(response_classification="positive")) is False

    def test_manual_unclassified_not_suppressed(self):
        # §3.16: manual_unclassified means we don't know yet — must NOT
        # auto-suppress (would be a silent false-positive).  Caller must
        # classify before deciding.
        assert is_suppressed(_entry(response_classification="manual_unclassified")) is False

    def test_active_stage_not_suppressed(self):
        # Active pipeline stages must not be flagged. Spot-check the
        # canonical send-flow stages.
        for stage in ("DM1 Sent", "DM2 Sent", "DM3 Sent", "Accepted", "Connection Sent"):
            assert is_suppressed(_entry(stage=stage)) is False, stage

    def test_suppress_flag_falsy_values(self):
        # None and False should not trigger; only truthy values.
        assert is_suppressed(_entry(suppress_re_engagement=None)) is False
        assert is_suppressed(_entry(suppress_re_engagement=False)) is False

    def test_or_semantics_any_single_signal(self):
        # Each signal alone is sufficient; together they still suppress.
        assert is_suppressed(_entry(
            suppress_re_engagement=True,
            response_classification="negative",
            stage="Not Interested",
        )) is True


class TestConstants:
    def test_negative_classifications_canonical(self):
        # Must include both negative and defensive; nothing else.
        assert frozenset({"negative", "defensive"}) == NEGATIVE_RESPONSE_CLASSIFICATIONS

    def test_suppressed_stages_canonical(self):
        # Must include both NOT_INTERESTED and DEFENSIVE_HOLD terminals.
        assert frozenset({"Not Interested", "Defensive Hold"}) == SUPPRESSED_STAGES


class TestBuildSuppressionSet:
    def test_collects_only_suppressed_record_ids(self, monkeypatch):
        monkeypatch.setenv(LINKEDIN_OUTREACH_LIST_ID_ENV, "list-uuid-001")
        attio = MagicMock()
        # Two suppressed (different signals), one clean.
        attio.query_list_entries.return_value = [
            {
                "id": {"entry_id": "e1", "record_id": "rec-A"},
                "entry_values": {
                    "stage": [{"status": {"title": "Not Interested"}}],
                },
            },
            {
                "id": {"entry_id": "e2", "record_id": "rec-B"},
                "entry_values": {
                    "response_classification": [{"value": "defensive"}],
                },
            },
            {
                "id": {"entry_id": "e3", "record_id": "rec-C"},
                "entry_values": {
                    "stage": [{"status": {"title": "DM1 Sent"}}],
                },
            },
        ]
        result = build_suppression_set(attio)
        assert result == {"rec-A", "rec-B"}

    def test_missing_list_id_env_raises(self, monkeypatch):
        # §0 #9: no silent fallback to empty set (would disable suppression).
        monkeypatch.delenv(LINKEDIN_OUTREACH_LIST_ID_ENV, raising=False)
        attio = MagicMock()
        with pytest.raises(RuntimeError, match="LinkedIn Outreach list ID not set"):
            build_suppression_set(attio)

    def test_empty_list_id_env_raises(self, monkeypatch):
        monkeypatch.setenv(LINKEDIN_OUTREACH_LIST_ID_ENV, "   ")
        attio = MagicMock()
        with pytest.raises(RuntimeError, match="LinkedIn Outreach list ID not set"):
            build_suppression_set(attio)

    def test_empty_entries_returns_empty_set(self, monkeypatch):
        monkeypatch.setenv(LINKEDIN_OUTREACH_LIST_ID_ENV, "list-uuid")
        attio = MagicMock()
        attio.query_list_entries.return_value = []
        assert build_suppression_set(attio) == set()

    def test_entries_with_no_record_id_skipped(self, monkeypatch):
        monkeypatch.setenv(LINKEDIN_OUTREACH_LIST_ID_ENV, "list-uuid")
        attio = MagicMock()
        attio.query_list_entries.return_value = [
            {
                "id": {"entry_id": "e1", "record_id": ""},
                "entry_values": {
                    "stage": [{"status": {"title": "Not Interested"}}],
                },
            },
        ]
        # parse_entry returns record_id="" → guard skips empty strings.
        assert build_suppression_set(attio) == set()


class TestApplySuppression:
    def test_writes_via_attio_writer_with_correct_intent(self):
        writer = MagicMock()
        result = apply_suppression(
            writer,
            entry_id="ent-42",
            list_id="list-uuid-001",
            prior_suppress=None,
        )
        assert result is True
        writer.apply.assert_called_once()
        intent = writer.apply.call_args[0][0]
        assert intent.object == "linkedin_outreach"
        assert intent.record_id == "ent-42"
        assert intent.is_list_entry is True
        assert intent.list_id == "list-uuid-001"
        assert intent.updates == {"suppress_re_engagement": True}
        assert intent.prior_values == {"suppress_re_engagement": None}
        assert intent.writer_module == "workflows.cross_channel_suppression"

    def test_writes_when_prior_is_false(self):
        # Existing False -> True is a real change; must write.
        writer = MagicMock()
        assert apply_suppression(
            writer, entry_id="ent-1", list_id="list-1", prior_suppress=False,
        ) is True
        writer.apply.assert_called_once()

    def test_idempotent_when_prior_true(self):
        writer = MagicMock()
        result = apply_suppression(
            writer,
            entry_id="ent-1",
            list_id="list-1",
            prior_suppress=True,
        )
        assert result is False
        writer.apply.assert_not_called()


class TestImportSurface:
    def test_module_imports_without_side_effects(self):
        # Importing the module must not require ATTIO_LIST_ID — only
        # build_suppression_set does. Critical because tests/test_*.py
        # may import workflows.cross_channel_suppression for any reason.
        #
        # Executed as a PRIVATE copy, NOT importlib.reload: reload rebinds the
        # live module's objects in place, so every module that already did
        # `from ... import EmailLaneNotProvisioned` keeps the pre-reload class
        # and its `except` clause silently stops matching — a landmine for
        # whichever test happens to run next.
        import importlib.util

        import workflows.cross_channel_suppression as mod

        spec = importlib.util.spec_from_file_location(
            "cross_channel_suppression_import_probe", mod.__file__,
        )
        probe = importlib.util.module_from_spec(spec)
        with patch.dict("os.environ", {}, clear=True):
            spec.loader.exec_module(probe)
        assert probe.WRITER_MODULE == "workflows.cross_channel_suppression"


class TestEmailHardDeclineIds:
    """§3.1 email-channel hard declines — fail CLOSED-HARD like
    build_suppression_set (a holed set could re-contact a hard no)."""

    def _attio_with_stages(
        self,
        by_stage: dict,
        broken: set | None = None,
        *,
        people_slugs: list[str] | None = None,
        schema_exc: Exception | None = None,
    ):
        attio = MagicMock()

        def search_people(filter_=None, limit=0, *, fail_if_truncated=False):
            stage = (filter_ or {}).get("email_campaign_stage")
            if broken and stage in broken:
                raise RuntimeError("attio 503")
            return [{"id": {"record_id": rid}} for rid in by_stage.get(stage, ())]

        def get_object_attributes(slug):
            # The schema probe that discriminates "attribute absent" from
            # "read failed". Default: attribute present (a fault is a fault).
            if schema_exc is not None:
                raise schema_exc
            slugs = ["email_campaign_stage"] if people_slugs is None else people_slugs
            return [{"api_slug": s} for s in slugs]

        attio.search_people.side_effect = search_people
        attio.get_object_attributes.side_effect = get_object_attributes
        return attio

    def test_collects_both_decline_stages(self):
        attio = self._attio_with_stages({
            "email_not_interested": ["rec-A"],
            "unsubscribed": ["rec-B"],
            "email_responded": ["rec-C"],  # NOT a decline — must not appear
        })
        assert email_hard_decline_ids(attio) == {"rec-A", "rec-B"}

    def test_declines_canonical(self):
        # Exactly the two hard-decline stages; RESPONDED must never join
        # (a live human-owned thread is not suppression).
        assert tuple(s.value for s in EMAIL_HARD_DECLINE_STAGES) == (
            "email_not_interested",
            "unsubscribed",
        )

    def test_partial_fetch_raises(self):
        # The attribute IS provisioned, so a query failure is a real fault.
        attio = self._attio_with_stages(
            {"email_not_interested": ["rec-A"]}, broken={"unsubscribed"},
        )
        with pytest.raises(RuntimeError, match="hard-decline"):
            email_hard_decline_ids(attio)

    def test_partial_fetch_is_not_a_provisioning_error(self):
        # A transient fault must NOT masquerade as "no email lane" — that
        # would turn an Attio outage into a silently skipped §3.1 set.
        attio = self._attio_with_stages(
            {"email_not_interested": ["rec-A"]}, broken={"unsubscribed"},
        )
        with pytest.raises(RuntimeError) as exc_info:
            email_hard_decline_ids(attio)
        assert not isinstance(exc_info.value, EmailLaneNotProvisioned)

    def test_absent_attribute_raises_email_lane_not_provisioned(self):
        # The optional email lane was never installed (this engine never
        # provisions people.email_campaign_stage), so every filter 400s.
        # Nothing is holed — the caller degrades instead of aborting.
        attio = self._attio_with_stages(
            {}, broken={"email_not_interested", "unsubscribed"},
            people_slugs=["name", "email_addresses"],
        )
        with pytest.raises(EmailLaneNotProvisioned, match="email_campaign_stage"):
            email_hard_decline_ids(attio)

    def test_unreadable_schema_keeps_failing_closed(self):
        # Probe failed → "unknown", which must never read as "absent".
        attio = self._attio_with_stages(
            {}, broken={"email_not_interested", "unsubscribed"},
            schema_exc=RuntimeError("attio 503"),
        )
        with pytest.raises(RuntimeError, match="hard-decline") as exc_info:
            email_hard_decline_ids(attio)
        assert not isinstance(exc_info.value, EmailLaneNotProvisioned)

    def test_schema_is_not_probed_on_the_happy_path(self):
        # The discriminator costs one GET and must only be paid on failure.
        attio = self._attio_with_stages({"email_not_interested": ["rec-A"]})
        assert email_hard_decline_ids(attio) == {"rec-A"}
        attio.get_object_attributes.assert_not_called()


class TestEmailCampaignStageProvisioned:
    def _attio(self, slugs=None, exc=None):
        attio = MagicMock()
        if exc is not None:
            attio.get_object_attributes.side_effect = exc
        else:
            attio.get_object_attributes.return_value = [
                {"api_slug": s} for s in (slugs or [])
            ]
        return attio

    def test_present(self):
        assert email_campaign_stage_provisioned(
            self._attio(["name", "email_campaign_stage"])
        ) is True

    def test_absent(self):
        assert email_campaign_stage_provisioned(self._attio(["name"])) is False

    def test_unreadable_is_none_not_false(self):
        # None ≠ False: callers must not read an outage as "no email lane".
        assert email_campaign_stage_provisioned(
            self._attio(exc=RuntimeError("attio 503"))
        ) is None


class TestEmailPersonIdsInStages:
    def test_partial_failure_returns_partial_set_and_flag(self):
        attio = MagicMock()

        def search_people(filter_=None, limit=0, *, fail_if_truncated=False):
            stage = (filter_ or {}).get("email_campaign_stage")
            if stage == "unsubscribed":
                raise RuntimeError("attio 503")
            return [{"id": {"record_id": "rec-A"}}]

        attio.search_people.side_effect = search_people
        ids, ok = email_person_ids_in_stages(
            attio, EMAIL_HARD_DECLINE_STAGES, "hard-decline"
        )
        # The helper itself fails soft — (partial ids, ok=False); each
        # wrapper picks its own philosophy on top.
        assert ids == {"rec-A"}
        assert ok is False

    def test_rows_without_record_id_skipped(self):
        attio = MagicMock()
        attio.search_people.return_value = [{"id": {}}, {}, {"id": {"record_id": "rec-A"}}]
        ids, ok = email_person_ids_in_stages(
            attio, EMAIL_HARD_DECLINE_STAGES, "hard-decline"
        )
        assert ids == {"rec-A"}
        assert ok is True
