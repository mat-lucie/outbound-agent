"""Tests for scripts/seed_botdog_blacklist.py.

Covers the CRM-read predicate (who's seeded / who isn't), the operator's
configured never-contact denylist (ALWAYS seeded regardless of pipeline
stage — a hard block), idempotent skip of already-blacklisted URLs,
dry-run writing nothing, batch chunking, and loud partial-failure
handling. The CRM and Botdog clients are mocked; no live calls anywhere.

The denylist is operator config, not code: `config/botdog.yaml` →
`blacklist.denylist_companies`. The suite resolves the synthetic Acme
reference operator (conftest's OUTBOUND_CONFIG_DIR pin), whose denylist is
a single token — "Contoso Holdings". Unit-level tests pass their own
`tokens` explicitly so they never depend on that file's contents;
orchestration tests exercise the real config-driven path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from clients.botdog import MAX_LEADS_PER_BATCH, BotdogError, blacklist_name
from models.pipeline import PipelineStage
from scripts.seed_botdog_blacklist import (
    UNRESOLVED_BUCKET,
    BlacklistResolutionError,
    SeedLead,
    apply_seed,
    classify_seed_category,
    collect_seed_leads,
    denylist_tokens,
    matches_denylist,
    resolve_blacklist,
    seed,
)

# The synthetic denylist used by the unit-level tests. Passed explicitly so
# these tests assert the FUNCTION's behavior, not the example config's.
TOKENS = ("contoso holdings", "contoso")


def _entry(record_id: str, stage: str, **extra) -> dict:
    return {"record_id": record_id, "stage": stage, **extra}


def _name() -> str:
    """The blacklist collection name the seeder resolves."""
    return blacklist_name()


# ---------------------------------------------------------------------------
# The denylist hard block — the red line
# ---------------------------------------------------------------------------


class TestDenylistHardBlock:
    def test_matches_denylist_on_company_and_name(self):
        assert matches_denylist("Contoso Holdings SA de CV", None, TOKENS)
        assert matches_denylist("CONTOSO", None, TOKENS)
        assert matches_denylist(None, "Someone at Contoso", TOKENS)
        assert not matches_denylist("Acme Foods", "Jane Doe", TOKENS)
        assert not matches_denylist(None, None, TOKENS)

    def test_no_configured_tokens_matches_nothing(self):
        """The denylist is operator config and is EMPTY by default: an
        operator who configured none must not have arbitrary rows
        hard-blocked (and the seeder must not fall back to a name baked
        into the engine)."""
        assert not matches_denylist("Contoso Holdings", "Jane Doe", ())

    @pytest.mark.parametrize("stage", [s.value for s in PipelineStage])
    def test_denylisted_row_always_seeded_regardless_of_stage(self, stage):
        """HARD block: a denylisted row is seeded at EVERY pipeline stage,
        including PROSPECT / PARTNER_INTRO where an ordinary row is not."""
        assert classify_seed_category(
            {"stage": stage}, "Contoso Holdings", None, TOKENS
        ) == "denylist"

    def test_denylist_beats_every_other_signal(self):
        """Precedence: even a merged/suppressed denylisted row classifies
        as denylist (the hard block is the headline category)."""
        assert classify_seed_category(
            {"stage": PipelineStage.PROSPECT.value,
             "merged_into": "x", "suppress_re_engagement": True},
            "Contoso", None, TOKENS,
        ) == "denylist"

    def test_denylisted_prospect_included_in_collected_leads(self):
        """End-to-end: a PROSPECT-stage denylisted row lands in the seed
        set (an ordinary PROSPECT row does not)."""
        entries = [
            _entry("r1", PipelineStage.PROSPECT.value),  # denylisted company
            _entry("r2", PipelineStage.PROSPECT.value),  # plain prospect
        ]
        info = {
            "r1": ("Alice", "Contoso Holdings",
                   "https://linkedin.com/in/acme-alice"),
            "r2": ("Bob", "Acme Foods", "https://linkedin.com/in/acme-bob"),
        }
        leads, breakdown, _unresolved = collect_seed_leads(entries, info, TOKENS)
        assert [lead.record_id for lead in leads] == ["r1"]
        assert leads[0].category == "denylist"
        assert breakdown["denylist"] == 1

    def test_denylist_tokens_come_from_operator_config(self):
        """The seeder reads the tokens from config, lowercased — no name is
        hardcoded in the engine."""
        tokens = denylist_tokens()
        assert all(token == token.lower() for token in tokens)
        assert "contoso holdings" in tokens


# ---------------------------------------------------------------------------
# The contacted / excluded predicate
# ---------------------------------------------------------------------------


class TestClassifyPredicate:
    def test_fresh_prospect_and_partner_intro_not_seeded(self):
        assert classify_seed_category(
            {"stage": PipelineStage.PROSPECT.value}, "Acme Foods", None, TOKENS
        ) is None
        assert classify_seed_category(
            {"stage": PipelineStage.PARTNER_INTRO.value},
            "Acme Foods", None, TOKENS,
        ) is None

    def test_contacted_stages_seeded(self):
        for stage in (
            PipelineStage.CONNECTION_SENT,
            PipelineStage.ACCEPTED,
            PipelineStage.DM1_SENT,
            PipelineStage.RESPONDED,
            PipelineStage.CALL_BOOKED,
            PipelineStage.QUALIFIED,
        ):
            assert classify_seed_category(
                {"stage": stage.value}, "Acme Foods", None, TOKENS
            ) == "contacted"

    def test_decline_stages_seeded_as_declined(self):
        for stage in (
            PipelineStage.NOT_INTERESTED,
            PipelineStage.DEFENSIVE_HOLD,
            PipelineStage.UNREACHABLE,
        ):
            assert classify_seed_category(
                {"stage": stage.value}, "Acme Foods", None, TOKENS
            ) == "declined"

    def test_merged_prospect_seeded(self):
        assert classify_seed_category(
            {"stage": PipelineStage.PROSPECT.value, "merged_into": "rec9"},
            "Acme Foods", None, TOKENS,
        ) == "merged"

    def test_suppressed_prospect_seeded(self):
        assert classify_seed_category(
            {"stage": PipelineStage.PROSPECT.value,
             "suppress_re_engagement": True},
            "Acme Foods", None, TOKENS,
        ) == "suppressed"

    def test_unknown_or_missing_stage_not_seeded(self):
        assert classify_seed_category(
            {"stage": None}, "Acme Foods", None, TOKENS
        ) is None
        assert classify_seed_category(
            {"stage": "Bogus"}, "Acme Foods", None, TOKENS
        ) is None


# ---------------------------------------------------------------------------
# collect_seed_leads — dedup, URL sourcing, breakdown
# ---------------------------------------------------------------------------


class TestCollectSeedLeads:
    def test_dedup_by_canonical_url(self):
        """Two rows resolving to the same profile (different URL forms)
        collapse to one lead."""
        entries = [
            _entry("r1", PipelineStage.CONNECTION_SENT.value,
                   canonical_linkedin_url="https://www.linkedin.com/in/AAA/"),
            _entry("r2", PipelineStage.DM1_SENT.value),
        ]
        info = {
            "r1": ("Alice", "Acme Foods", "https://linkedin.com/in/aaa"),
            "r2": ("Alice", "Acme Foods", "https://linkedin.com/in/aaa"),
        }
        leads, _bd, _unresolved = collect_seed_leads(entries, info, TOKENS)
        assert [lead.canonical_url for lead in leads] == [
            "https://linkedin.com/in/aaa"
        ]

    def test_entry_canonical_url_preferred_over_person_url(self):
        entries = [
            _entry("r1", PipelineStage.CONNECTION_SENT.value,
                   canonical_linkedin_url="https://linkedin.com/in/entry"),
        ]
        info = {"r1": ("Alice", "Acme Foods",
                       "https://linkedin.com/in/person")}
        leads, _bd, _unresolved = collect_seed_leads(entries, info, TOKENS)
        assert leads[0].canonical_url == "https://linkedin.com/in/entry"

    def test_row_with_no_url_counted_not_dropped_silently(self):
        entries = [_entry("r1", PipelineStage.DM1_SENT.value)]
        info = {"r1": ("Alice", "Acme Foods", "")}  # no url anywhere
        leads, breakdown, _unresolved = collect_seed_leads(entries, info, TOKENS)
        assert leads == []
        assert breakdown["skipped_no_url"] == 1


# ---------------------------------------------------------------------------
# resolve_blacklist — fetch-or-create + existing-URL extraction
# ---------------------------------------------------------------------------


class TestResolveBlacklist:
    def test_reuses_existing_blacklist_and_reads_present_urls(self):
        botdog = MagicMock()
        botdog.get_blacklists.return_value = [
            {"id": "bl_1", "name": _name(), "leadCount": 1},
        ]
        # Present URLs come from the dedicated entries endpoint
        # (GET /v1/blacklist/{id}/leads), `linkedinProfile`-keyed.
        botdog.get_blacklist_leads.return_value = [
            {"id": "l_1",
             "linkedinProfile": "https://www.linkedin.com/in/ACME-ALICE/"},
        ]
        bl_id, present = resolve_blacklist(botdog, _name())
        assert bl_id == "bl_1"
        assert present == {"https://linkedin.com/in/acme-alice"}
        botdog.get_blacklist_leads.assert_called_once_with("bl_1")
        botdog.create_blacklist.assert_not_called()

    def test_picks_populated_over_empty_duplicate_and_reads_its_entries(self):
        """An empty duplicate of the seeded collection can sit beside it.
        resolve_blacklist must target the POPULATED one and read ITS
        entries — never seed into the empty duplicate, or the seed and the
        pre-send gate would resolve to different collections."""
        botdog = MagicMock()
        botdog.get_blacklists.return_value = [
            {"id": "empty", "name": _name(), "leadCount": 0},
            {"id": "full", "name": _name(), "leadCount": 1462},
        ]
        botdog.get_blacklist_leads.return_value = []
        bl_id, _present = resolve_blacklist(botdog, _name())
        assert bl_id == "full"
        botdog.get_blacklist_leads.assert_called_once_with("full")
        botdog.create_blacklist.assert_not_called()

    def test_entries_read_failure_degrades_to_empty_present(self):
        """A BotdogError reading entries disables the skip (re-adds are
        safe, Botdog dedups) rather than aborting the seed."""
        botdog = MagicMock()
        botdog.get_blacklists.return_value = [
            {"id": "bl_1", "name": _name(), "leadCount": 5},
        ]
        botdog.get_blacklist_leads.side_effect = BotdogError("504 timeout")
        bl_id, present = resolve_blacklist(botdog, _name())
        assert bl_id == "bl_1"
        assert present == set()

    def test_creates_when_absent(self):
        """Create, then re-fetch: the id comes from the LISTING, not from
        the unverified create-response DTO."""
        botdog = MagicMock()
        botdog.get_blacklists.side_effect = [
            [],
            [{"id": "bl_new", "name": _name()}],
        ]
        botdog.create_blacklist.return_value = {"id": "bl_new"}
        botdog.get_blacklist_leads.return_value = []
        bl_id, present = resolve_blacklist(botdog, _name())
        assert bl_id == "bl_new"
        assert present == set()
        botdog.create_blacklist.assert_called_once_with(_name())

    def test_create_that_does_not_land_exits_loudly(self):
        """The create reported success but the collection is not in the
        listing — seeding would write into a collection nobody reads.
        Never report success."""
        botdog = MagicMock()
        botdog.get_blacklists.side_effect = [[], []]
        botdog.create_blacklist.return_value = {"weird": "shape"}
        with pytest.raises(BlacklistResolutionError) as exc:
            resolve_blacklist(botdog, _name())
        assert "found 0" in str(exc.value)
        assert "weird" in str(exc.value)  # raw response snippet

    def test_duplicate_collections_after_create_exit_loudly(self):
        """A DTO shape-miss that mints a SECOND collection splits the
        never-contact set in half, and the pre-send gate would then pass on
        whichever one it found."""
        botdog = MagicMock()
        botdog.get_blacklists.side_effect = [
            [],
            [
                {"id": "bl_a", "name": _name()},
                {"id": "bl_b", "name": _name().upper()},
            ],
        ]
        botdog.create_blacklist.return_value = {"id": "bl_b"}
        with pytest.raises(BlacklistResolutionError) as exc:
            resolve_blacklist(botdog, _name())
        assert "found 2" in str(exc.value)
        assert "DUPLICATE" in str(exc.value)

    def test_existing_collection_is_not_re_fetched(self):
        """The exactly-one assertion is a POST-CREATE guard — the happy
        path stays one API call."""
        botdog = MagicMock()
        botdog.get_blacklists.return_value = [
            {"id": "bl_1", "name": _name()},
        ]
        botdog.get_blacklist_leads.return_value = []
        resolve_blacklist(botdog, _name())
        assert botdog.get_blacklists.call_count == 1


# ---------------------------------------------------------------------------
# apply_seed — batching + loud partial-failure
# ---------------------------------------------------------------------------


def _leads(n: int) -> list[SeedLead]:
    return [
        SeedLead(canonical_url=f"https://linkedin.com/in/acme-p{i}",
                 category="contacted", record_id=f"r{i}")
        for i in range(n)
    ]


class TestApplySeed:
    def test_chunks_at_batch_cap(self):
        botdog = MagicMock()
        leads = _leads(MAX_LEADS_PER_BATCH + 5)
        added, failures = apply_seed(botdog, "bl_1", leads)
        assert added == MAX_LEADS_PER_BATCH + 5
        assert failures == []
        assert botdog.add_to_blacklist.call_count == 2
        first_batch = botdog.add_to_blacklist.call_args_list[0].args[1]
        assert len(first_batch) == MAX_LEADS_PER_BATCH
        assert first_batch[0] == {
            "linkedinUrl": "https://linkedin.com/in/acme-p0"
        }

    def test_partial_failure_is_loud_and_continues(self):
        """One failing batch is recorded; other batches still land, and the
        failure surfaces (never a silent partial seed)."""
        botdog = MagicMock()
        botdog.add_to_blacklist.side_effect = [
            BotdogError("boom", status_code=500),
            {"ok": True},
        ]
        added, failures = apply_seed(
            botdog, "bl_1", _leads(MAX_LEADS_PER_BATCH + 3)
        )
        assert added == 3  # only the second batch landed
        assert len(failures) == 1
        assert failures[0]["batch_size"] == MAX_LEADS_PER_BATCH


# ---------------------------------------------------------------------------
# seed() orchestration — dry-run writes nothing; apply skips present URLs
# ---------------------------------------------------------------------------


class TestSeedOrchestration:
    def _attio(self, monkeypatch):
        attio = MagicMock()
        attio.query_list_entries.return_value = ["raw1", "raw2"]
        monkeypatch.setattr(
            "scripts.seed_botdog_blacklist.AttioClient.parse_entry",
            lambda entry: {
                "raw1": _entry("r1", PipelineStage.DM1_SENT.value),
                "raw2": _entry("r2", PipelineStage.PROSPECT.value),
            }[entry],
        )
        monkeypatch.setattr(
            "scripts.seed_botdog_blacklist.resolve_record_info",
            lambda a, ids: {
                "r1": ("Alice", "Acme Foods",
                       "https://linkedin.com/in/acme-alice"),
                "r2": ("Bob", "Acme Foods",
                       "https://linkedin.com/in/acme-bob"),
            },
        )
        return attio

    def test_dry_run_writes_nothing(self, monkeypatch):
        attio = self._attio(monkeypatch)
        botdog = MagicMock()
        report = seed(attio, botdog, dry_run=True)
        assert report["seed_size"] == 1  # only the DM1 row, not the prospect
        assert report["added"] == 0
        botdog.add_to_blacklist.assert_not_called()
        botdog.create_blacklist.assert_not_called()

    def test_apply_skips_already_present(self, monkeypatch):
        attio = self._attio(monkeypatch)
        botdog = MagicMock()
        botdog.get_blacklists.return_value = [
            {"id": "bl_1", "name": _name(), "leadCount": 1},
        ]
        botdog.get_blacklist_leads.return_value = [
            {"id": "l_a",
             "linkedinProfile": "https://linkedin.com/in/acme-alice"},
        ]
        report = seed(attio, botdog, dry_run=False)
        assert report["skipped_already_present"] == 1
        assert report["added"] == 0  # the one lead was already present
        botdog.add_to_blacklist.assert_not_called()
        assert report["failures"] == []


# ---------------------------------------------------------------------------
# unresolved_identity — fail closed
# ---------------------------------------------------------------------------


class TestUnresolvedIdentityBucket:
    """A not-seeded row whose company AND person name are both empty was
    cleared past the denylist hard block on NO evidence — it could be a
    denylisted organisation."""

    def test_unresolvable_unseeded_row_lands_in_the_bucket(self):
        entries = [_entry("r1", PipelineStage.PROSPECT.value)]
        info = {"r1": (None, None, "https://linkedin.com/in/acme-alice")}
        leads, _bd, unresolved = collect_seed_leads(entries, info, TOKENS)
        assert leads == []
        assert unresolved == ["r1"]

    def test_blank_strings_count_as_unresolved(self):
        entries = [_entry("r1", PipelineStage.PROSPECT.value)]
        info = {"r1": ("  ", "", "https://linkedin.com/in/acme-alice")}
        _leads_out, _bd, unresolved = collect_seed_leads(entries, info, TOKENS)
        assert unresolved == ["r1"]

    def test_a_row_missing_from_record_info_is_unresolved(self):
        entries = [_entry("r1", PipelineStage.PROSPECT.value)]
        _leads_out, _bd, unresolved = collect_seed_leads(entries, {}, TOKENS)
        assert unresolved == ["r1"]

    def test_resolvable_rows_are_unaffected(self):
        entries = [
            _entry("r1", PipelineStage.PROSPECT.value),
            _entry("r2", PipelineStage.DM1_SENT.value),
        ]
        info = {
            "r1": ("Alice", "Acme Foods",
                   "https://linkedin.com/in/acme-alice"),
            "r2": ("Bob", "Acme Foods", "https://linkedin.com/in/acme-bob"),
        }
        leads, _bd, unresolved = collect_seed_leads(entries, info, TOKENS)
        assert unresolved == []
        assert [lead.record_id for lead in leads] == ["r2"]

    def test_seeded_row_with_no_identity_is_not_bucketed(self):
        """Scoped to NOT-seeded rows: a seeded row is blacklisted whatever
        its identity, so its blank company changes no outcome."""
        entries = [_entry("r1", PipelineStage.DM1_SENT.value)]
        info = {"r1": (None, None, "https://linkedin.com/in/acme-alice")}
        leads, _bd, unresolved = collect_seed_leads(entries, info, TOKENS)
        assert unresolved == []
        assert [lead.record_id for lead in leads] == ["r1"]

    def test_no_denylist_configured_leaves_the_bucket_empty(self):
        """The bucket exists only to protect the denylist check. With no
        denylist configured there is nothing a blank identity could have
        cleared, so bucketing every identity-less prospect would refuse
        `--apply` for an operator who never asked for a hard block."""
        entries = [_entry("r1", PipelineStage.PROSPECT.value)]
        info = {"r1": (None, None, "https://linkedin.com/in/acme-alice")}
        leads, _bd, unresolved = collect_seed_leads(entries, info, ())
        assert unresolved == []
        assert leads == []


class TestUnresolvedIdentityRefusesApply:
    def _attio(self, monkeypatch, info):
        attio = MagicMock()
        attio.query_list_entries.return_value = ["raw1", "raw2"]
        monkeypatch.setattr(
            "scripts.seed_botdog_blacklist.AttioClient.parse_entry",
            lambda entry: {
                "raw1": _entry("r1", PipelineStage.DM1_SENT.value),
                "raw2": _entry("r2", PipelineStage.PROSPECT.value),
            }[entry],
        )
        monkeypatch.setattr(
            "scripts.seed_botdog_blacklist.resolve_record_info",
            lambda a, ids: info,
        )
        return attio

    _UNRESOLVED_INFO = {
        "r1": ("Alice", "Acme Foods", "https://linkedin.com/in/acme-alice"),
        # No identity at all — could be the denylisted organisation.
        "r2": (None, None, "https://linkedin.com/in/acme-bob"),
    }

    def test_dry_run_reports_the_bucket_prominently(self, monkeypatch, capsys):
        attio = self._attio(monkeypatch, self._UNRESOLVED_INFO)
        report = seed(attio, MagicMock(), dry_run=True)
        assert report[UNRESOLVED_BUCKET]["count"] == 1
        assert report[UNRESOLVED_BUCKET]["sample_record_ids"] == ["r2"]
        err = capsys.readouterr().err
        assert UNRESOLVED_BUCKET.upper() in err
        assert "denylisted organisation" in err

    def test_apply_refuses_and_writes_nothing(self, monkeypatch, capsys):
        attio = self._attio(monkeypatch, self._UNRESOLVED_INFO)
        botdog = MagicMock()
        report = seed(attio, botdog, dry_run=False)
        assert report["refused"] == UNRESOLVED_BUCKET
        assert report["added"] == 0
        botdog.get_blacklists.assert_not_called()
        botdog.create_blacklist.assert_not_called()
        botdog.add_to_blacklist.assert_not_called()
        assert "REFUSING to --apply" in capsys.readouterr().err

    def test_apply_proceeds_when_the_bucket_is_empty(self, monkeypatch):
        attio = self._attio(monkeypatch, {
            "r1": ("Alice", "Acme Foods",
                   "https://linkedin.com/in/acme-alice"),
            "r2": ("Bob", "Acme Foods", "https://linkedin.com/in/acme-bob"),
        })
        botdog = MagicMock()
        botdog.get_blacklists.return_value = [
            {"id": "bl_1", "name": _name(), "leads": []},
        ]
        report = seed(attio, botdog, dry_run=False)
        assert "refused" not in report
        assert report["added"] == 1
        botdog.add_to_blacklist.assert_called_once()

    def test_apply_proceeds_with_no_denylist_despite_blank_identity(
        self, monkeypatch
    ):
        """The other half of the fail-closed branch, at orchestration
        level: an operator with no configured denylist is never blocked by
        a row whose identity the CRM cannot resolve."""
        import scripts.seed_botdog_blacklist as sbb

        monkeypatch.setattr(sbb, "denylist_tokens", lambda: ())
        attio = self._attio(monkeypatch, self._UNRESOLVED_INFO)
        botdog = MagicMock()
        botdog.get_blacklists.return_value = [
            {"id": "bl_1", "name": _name(), "leads": []},
        ]
        report = seed(attio, botdog, dry_run=False)
        assert UNRESOLVED_BUCKET not in report
        assert "refused" not in report
        assert report["added"] == 1
        botdog.add_to_blacklist.assert_called_once()


class TestBlacklistIdBypass:
    """`--blacklist-id` is the outage bypass: `GET /blacklist` can 504
    server-side while POST works, so an explicit id skips resolution."""

    def test_bypass_skips_resolution_and_seeds(self, monkeypatch):
        from unittest.mock import patch

        import scripts.seed_botdog_blacklist as sbb

        attio = MagicMock()
        attio.query_list_entries.return_value = ["raw1"]
        monkeypatch.setattr(
            "clients.attio.AttioClient.parse_entry",
            lambda entry: _entry("r1", PipelineStage.DM1_SENT.value),
        )
        monkeypatch.setattr(
            "scripts.seed_botdog_blacklist.resolve_record_info",
            lambda a, ids: {
                "r1": ("Alice", "Acme Foods",
                       "https://linkedin.com/in/acme-alice"),
            },
        )
        botdog = MagicMock()
        with patch.object(sbb, "resolve_blacklist") as resolver:
            report = sbb.seed(
                attio, botdog, dry_run=False, blacklist_id="bl-explicit"
            )
        resolver.assert_not_called()
        assert report["blacklist_id"] == "bl-explicit"
        assert report["added"] == 1
        botdog.add_to_blacklist.assert_called_once()
        assert botdog.add_to_blacklist.call_args.args[0] == "bl-explicit"


class TestAsciiSafeUrl:
    def test_ascii_urls_pass_through(self):
        from scripts.seed_botdog_blacklist import _ascii_safe_url

        u = "https://linkedin.com/in/acme-jane-doe-123"
        assert _ascii_safe_url(u) == u

    def test_unicode_slug_is_percent_encoded(self):
        """A non-ASCII vanity slug (accents, flag emoji) makes Botdog's URL
        validator 400 the ENTIRE batch; the percent-encoded form is
        accepted."""
        from scripts.seed_botdog_blacklist import _ascii_safe_url

        out = _ascii_safe_url(
            "https://linkedin.com/in/acme-joão-🇧🇷-0001"
        )
        assert out.isascii()
        assert out.startswith("https://linkedin.com/in/acme-jo%C3%A3o-")

    def test_already_encoded_not_double_encoded(self):
        from scripts.seed_botdog_blacklist import _ascii_safe_url

        u = "https://linkedin.com/in/acme-jo%C3%A3o-santos"
        assert _ascii_safe_url(u) == u
