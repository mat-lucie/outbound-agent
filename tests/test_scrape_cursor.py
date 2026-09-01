"""Tests for the pipeline-owned ingest cursor (the weekly-recycling RCA).

Three halves:

  * `workflows/scrape_cursor.py` in isolation — the state file contract and
    its two integrity anchors (search-URL fingerprint, file-prefix row).
  * `_launch_and_download`'s consume semantics — the delta slice and the
    loud guards. It reads the cursor; it never writes it.
  * The weekly search loop — where the cursor actually advances, and only
    after `_process_prospects` has returned without raising.
"""

import json
from unittest.mock import MagicMock
from unittest.mock import patch as _patch

import pytest

from workflows.scrape_cursor import (
    CursorStateCorruptError,
    advance_cursor,
    read_cursor,
    read_cursor_state,
)

SN_URL = "https://linkedin.com/sales/search/x"
CSV_NAME = "wk-operations-leaders-mexico"


def _csv(n_rows: int, start: int = 0) -> str:
    """`n_rows` SN-shaped rows, names numbered from `start` so a delta slice
    can be asserted by identity rather than by count alone."""
    header = "firstName,lastName,defaultProfileUrl\n"
    body = "".join(
        f"Person{i},Test,https://www.linkedin.com/in/p{i}\n"
        for i in range(start, start + n_rows)
    )
    return header + body


class TestCursorStateFile:
    def test_missing_file_reads_zero(self, tmp_path):
        assert read_cursor("wk-a-b", tmp_path / "nope.json") == 0

    def test_missing_key_reads_zero(self, tmp_path):
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 42, path)
        assert read_cursor("wk-other-search", path) == 0

    def test_round_trip(self, tmp_path):
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 300, path)
        assert read_cursor("wk-a-b", path) == 300
        advance_cursor("wk-a-b", 380, path)
        assert read_cursor("wk-a-b", path) == 380

    def test_multiple_searches_are_independent(self, tmp_path):
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 100, path)
        advance_cursor("wk-c-d", 7, path)
        assert read_cursor("wk-a-b", path) == 100
        assert read_cursor("wk-c-d", path) == 7

    def test_advance_stamps_updated_at(self, tmp_path):
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 5, path)
        entry = json.loads(path.read_text())["wk-a-b"]
        assert entry["consumed_rows"] == 5
        assert entry["updated_at"]

    def test_corrupt_json_raises(self, tmp_path):
        """A corrupt file must NOT degrade to 0 — a silent 0 re-ingests an
        entire accumulating search."""
        path = tmp_path / "scrape_cursors.json"
        path.write_text("{not json at all")
        with pytest.raises(CursorStateCorruptError):
            read_cursor("wk-a-b", path)

    def test_empty_file_raises(self, tmp_path):
        """Emptiness means a truncated write — `advance_cursor` only ever
        lands complete files via os.replace."""
        path = tmp_path / "scrape_cursors.json"
        path.write_text("")
        with pytest.raises(CursorStateCorruptError):
            read_cursor("wk-a-b", path)

    def test_non_object_top_level_raises(self, tmp_path):
        path = tmp_path / "scrape_cursors.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(CursorStateCorruptError):
            read_cursor("wk-a-b", path)

    def test_bad_entry_shape_raises(self, tmp_path):
        path = tmp_path / "scrape_cursors.json"
        path.write_text(json.dumps({"wk-a-b": 300}))
        with pytest.raises(CursorStateCorruptError):
            read_cursor("wk-a-b", path)

    @pytest.mark.parametrize("bad", ["300", -1, None, True])
    def test_bad_consumed_rows_raises(self, tmp_path, bad):
        path = tmp_path / "scrape_cursors.json"
        path.write_text(json.dumps({"wk-a-b": {"consumed_rows": bad}}))
        with pytest.raises(CursorStateCorruptError):
            read_cursor("wk-a-b", path)

    def test_advance_creates_parent_dir(self, tmp_path):
        path = tmp_path / "exports" / "scrape_cursors.json"
        advance_cursor("wk-a-b", 3, path)
        assert read_cursor("wk-a-b", path) == 3

    def test_advance_leaves_no_tmp_files(self, tmp_path):
        """Atomicity is a tmp-file + os.replace; assert the tmp file doesn't
        survive (the exports dir is read by eye by operators)."""
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 3, path)
        assert [p.name for p in tmp_path.iterdir()] == ["scrape_cursors.json"]

    def test_advance_rejects_negative(self, tmp_path):
        with pytest.raises(ValueError):
            advance_cursor("wk-a-b", -1, tmp_path / "scrape_cursors.json")

    def test_advance_rejects_non_int(self, tmp_path):
        with pytest.raises(TypeError):
            advance_cursor("wk-a-b", True, tmp_path / "scrape_cursors.json")

    def test_default_path_is_anchored_to_the_repo_root(self):
        """cwd-relative would silently start a FRESH cursor whenever the
        weekly is launched from a different directory (cron vs skill vs
        hand), re-ingesting every accumulating file."""
        from pathlib import Path

        from workflows import scrape_cursor

        # The autouse conftest fixture rebinds DEFAULT_CURSOR_PATH, so assert
        # against the module's own anchor instead.
        expected = Path(scrape_cursor.__file__).resolve().parent.parent
        assert expected == scrape_cursor._REPO_ROOT
        assert scrape_cursor._REPO_ROOT.is_absolute()


class TestCursorIntegrityAnchors:
    def test_url_fingerprint_is_stored_and_matches(self, tmp_path):
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 300, path, sn_url=SN_URL)
        entry = json.loads(path.read_text())["wk-a-b"]
        assert len(entry["sn_url_sha8"]) == 8
        state = read_cursor_state("wk-a-b", path, sn_url=SN_URL)
        assert state.consumed_rows == 300
        assert state.url_changed is False

    def test_url_mismatch_resets_to_zero(self, tmp_path):
        """The saved search behind a csvName was swapped: our count indexes
        the OLD search's rows, so carrying it forward would skip the new
        search's first N people forever."""
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 300, path, sn_url=SN_URL)
        state = read_cursor_state("wk-a-b", path, sn_url="https://sn/DIFFERENT")
        assert state.consumed_rows == 0
        assert state.url_changed is True

    def test_missing_url_anchor_is_backward_compatible(self, tmp_path):
        """Entries written before the anchor existed must keep working."""
        path = tmp_path / "scrape_cursors.json"
        path.write_text(json.dumps({"wk-a-b": {"consumed_rows": 300}}))
        state = read_cursor_state("wk-a-b", path, sn_url=SN_URL)
        assert state.consumed_rows == 300
        assert state.url_changed is False

    def test_last_row_url_anchor_round_trips(self, tmp_path):
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 2, path, last_row_url="https://li/in/p1")
        assert read_cursor_state("wk-a-b", path).last_row_url == "https://li/in/p1"

    def test_blank_last_row_url_is_not_stored(self, tmp_path):
        """An empty anchor must read back as None (skip the check) rather
        than as an anchor that can never match."""
        path = tmp_path / "scrape_cursors.json"
        advance_cursor("wk-a-b", 2, path, last_row_url="")
        assert read_cursor_state("wk-a-b", path).last_row_url is None


class TestWeeklyCsvNameDerivation:
    def test_slugifies_persona_and_geo(self):
        from workflows.weekly_prospect import weekly_csv_name

        assert (
            weekly_csv_name("us_operations_leaders", "us_1")
            == "wk-us-operations-leaders-us-1"
        )

    def test_stable_across_calls(self):
        from workflows.weekly_prospect import weekly_csv_name

        first = weekly_csv_name("operations_leaders", "mexico")
        assert first == weekly_csv_name("operations_leaders", "mexico")
        # No timestamp / run id leaked into the name — stability is the fix.
        assert first == "wk-operations-leaders-mexico"

    def test_strips_unsafe_characters(self):
        from workflows.weekly_prospect import weekly_csv_name

        assert (
            weekly_csv_name("Ops Leaders!", "MX/North_2") == "wk-ops-leaders-mx-north-2"
        )

    def test_distinct_searches_get_distinct_names(self):
        from workflows.weekly_prospect import weekly_csv_name

        assert weekly_csv_name("a", "mexico") != weekly_csv_name("a", "chile")


class TestWeeklyConsumeDelta:
    """`_launch_and_download` hands scoring only the rows we have not
    consumed before. It READS the cursor; the caller advances it."""

    def _pb(self, csv_text):
        pb = MagicMock()
        pb.launch_agent.return_value = MagicMock()
        pb.download_result_csv.return_value = csv_text
        return pb

    def _call(self, wp, pb, **kwargs):
        return wp._launch_and_download(
            pb, "agent-1", SN_URL, 100,
            persona_key="operations_leaders", geo_key="mexico", **kwargs,
        )

    @pytest.fixture
    def wp(self, monkeypatch):
        import workflows.weekly_prospect as module

        monkeypatch.setenv("PB_LI_SESSION_COOKIE", "cookie-abc")
        # The cursor state file is redirected to a per-test temp path by the
        # autouse `_isolate_scrape_cursors` fixture in conftest.
        return module

    def _consume(self, wp, csv_text):
        """Run one full ingest: download the delta, then advance as the
        weekly loop does once `_process_prospects` has returned."""
        delta = self._call(wp, self._pb(csv_text))
        if delta.rows:
            advance_cursor(
                delta.csv_name, delta.file_total, sn_url=SN_URL,
                last_row_url=wp._row_profile_url(delta.rows[-1]),
            )
        return delta

    def test_first_run_consumes_everything(self, wp):
        delta = self._consume(wp, _csv(300))

        assert len(delta.rows) == 300
        assert delta.rows[0]["firstName"] == "Person0"
        assert delta.file_total == 300
        assert read_cursor(CSV_NAME) == 300

    def test_second_run_consumes_only_the_delta(self, wp):
        self._consume(wp, _csv(300))
        # PB appended 80 rows to the SAME accumulating file.
        delta = self._consume(wp, _csv(380))

        assert len(delta.rows) == 80
        assert delta.rows[0]["firstName"] == "Person300"
        assert delta.rows[-1]["firstName"] == "Person379"
        assert read_cursor(CSV_NAME) == 380

    def test_launch_never_advances_the_cursor_itself(self, wp):
        """The advance lives in the caller, after ingest. If it crept back
        into the downloader, a mid-scoring crash would lose the rows."""
        self._call(wp, self._pb(_csv(300)))
        assert read_cursor(CSV_NAME) == 0

    def test_zero_delta_returns_nothing_and_warns(self, wp, capsys):
        self._consume(wp, _csv(300))
        delta = self._consume(wp, _csv(300))

        assert delta.rows == []
        assert delta.status is wp.ScrapeStatus.ZERO_DELTA
        err = capsys.readouterr().err
        assert "ZERO-DELTA" in err
        assert CSV_NAME in err
        # Not advanced, not rewound — the file is unchanged.
        assert read_cursor(CSV_NAME) == 300

    def test_shrunk_file_reconsumes_everything_and_warns(self, wp, capsys):
        self._consume(wp, _csv(300))
        # PB storage reset: the result file was recreated with 120 rows. PB's
        # own resume cursor lives in that storage too, so page 1 is correct.
        delta = self._consume(wp, _csv(120))

        assert len(delta.rows) == 120
        assert delta.rows[0]["firstName"] == "Person0"
        assert delta.cursor_reset is True
        err = capsys.readouterr().err
        assert "CURSOR RESET" in err
        assert "ZERO-DELTA" not in err
        assert read_cursor(CSV_NAME) == 120

    def test_search_url_change_resets_and_warns(self, wp, capsys):
        """Same csvName, different saved search: the old count indexes rows
        that are no longer there."""
        self._consume(wp, _csv(300))

        pb = self._pb(_csv(300, start=900))
        delta = wp._launch_and_download(
            pb, "agent-1", "https://linkedin.com/sales/search/REPLACED", 100,
            persona_key="operations_leaders", geo_key="mexico",
        )

        assert len(delta.rows) == 300
        assert delta.rows[0]["firstName"] == "Person900"
        assert delta.cursor_reset is True
        err = capsys.readouterr().err
        assert "SEARCH URL CHANGED" in err
        assert "consider a fresh csvName" in err

    def test_file_prefix_change_resets_and_warns(self, wp, capsys):
        """A rebuilt file of the same length is invisible to a row count —
        the last-consumed-row anchor is what catches it."""
        self._consume(wp, _csv(300))
        # Same 300 rows' worth of length, entirely different people.
        delta = self._consume(wp, _csv(400, start=5000))

        assert len(delta.rows) == 400
        assert delta.rows[0]["firstName"] == "Person5000"
        assert delta.cursor_reset is True
        err = capsys.readouterr().err
        assert "FILE PREFIX CHANGED" in err

    def test_missing_prefix_anchor_skips_the_check(self, wp):
        """Old entries carry no anchor; they must slice normally, not reset."""
        advance_cursor(CSV_NAME, 300, sn_url=SN_URL)
        delta = self._call(wp, self._pb(_csv(380)))

        assert len(delta.rows) == 80
        assert delta.cursor_reset is False

    def test_empty_download_returns_no_data_without_advancing(self, wp):
        self._consume(wp, _csv(300))
        delta = self._call(wp, self._pb(None))
        assert delta.rows == []
        assert delta.status is wp.ScrapeStatus.NO_DATA
        assert read_cursor(CSV_NAME) == 300

    def test_missing_cookie_returns_no_data_without_launching(self, wp, monkeypatch):
        """A config miss must not look like a drained search — and must not
        burn a PB launch."""
        monkeypatch.delenv("PB_LI_SESSION_COOKIE", raising=False)
        pb = self._pb(_csv(10))
        delta = self._call(wp, pb)

        assert delta.status is wp.ScrapeStatus.NO_DATA
        assert delta.rows == []
        assert pb.launch_agent.call_count == 0

    def test_delta_preserves_quoted_multiline_fields(self, wp):
        """Row N is not line N — SN exports carry quoted embedded newlines,
        so the delta slice must go through a real CSV parse."""
        first = (
            'firstName,lastName,defaultProfileUrl,note\n'
            'A,One,https://li/in/a,"line1\nline2"\n'
        )
        self._consume(wp, first)
        assert read_cursor(CSV_NAME) == 1

        second = first + 'B,Two,https://li/in/b,"x\ny\nz"\n'
        delta = self._consume(wp, second)

        assert len(delta.rows) == 1
        assert delta.rows[0]["firstName"] == "B"
        assert delta.rows[0]["note"] == "x\ny\nz"

    def test_ragged_row_flows_through_with_restkey_stripped(self, wp):
        """PB occasionally serves a row with MORE fields than the header.
        The old CSV round-trip crashed DictWriter on it; parsed rows must
        pass through, with DictReader's None restkey dropped so downstream
        `.get()` callers see a plain str-keyed dict."""
        ragged = (
            "firstName,lastName,defaultProfileUrl\n"
            "A,One,https://li/in/a\n"
            "B,Two,https://li/in/b,extra1,extra2\n"
        )
        delta = self._consume(wp, ragged)

        assert len(delta.rows) == 2
        assert all(None not in row for row in delta.rows)
        assert delta.rows[1]["firstName"] == "B"
        assert read_cursor(CSV_NAME) == 2

    def test_use_cursor_false_neither_reads_nor_writes_state(self, wp, capsys):
        """The preview path: it must return every row and leave production
        cursor state exactly as it found it."""
        self._consume(wp, _csv(300))

        delta = self._call(wp, self._pb(_csv(300)), use_cursor=False)

        assert len(delta.rows) == 300
        assert delta.rows[0]["firstName"] == "Person0"
        assert delta.cursor_reset is False
        assert read_cursor(CSV_NAME) == 300
        err = capsys.readouterr().err
        assert "ZERO-DELTA" not in err

    def test_use_cursor_false_ignores_a_corrupt_state_file(self, wp):
        """No read at all means a damaged state file cannot break a preview."""
        from workflows import scrape_cursor

        scrape_cursor.DEFAULT_CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        scrape_cursor.DEFAULT_CURSOR_PATH.write_text("{ garbage")

        delta = self._call(wp, self._pb(_csv(5)), use_cursor=False)
        assert len(delta.rows) == 5


class TestWeeklyLoopCursorCommit:
    """The advance is the caller's job, and it happens only after
    `_process_prospects` returns without raising."""

    CSV = (
        "fullName,title,companyName,location,defaultProfileUrl\n"
        "Test User,Plant Manager,Subsidiary,\"Mexico City, Mexico\","
        "https://www.linkedin.com/in/loop-cursor\n"
    )

    def _personas(self, sn_urls=None):
        return {
            "operations_leaders": {
                "enterprise_mode": True, "search_size_credit": 15,
                "search_queries": {
                    "sn_search_urls": sn_urls or {"mx": SN_URL}
                },
            }
        }

    def _crm(self):
        crm = MagicMock()
        crm.query_list_entries.return_value = []
        crm.search_person_by_linkedin.return_value = None
        return crm

    def _run(
        self, monkeypatch, tmp_path, *, deltas, process_side_effect=None,
        personas=None,
    ):
        from workflows import weekly_prospect as wp

        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        monkeypatch.setattr(wp, "EXPORTS_DIR", tmp_path)

        stack = [
            _patch.object(wp, "build_anthropic_client", return_value=None),
            _patch.object(wp, "load_personas",
                          return_value=personas or self._personas()),
            _patch.object(wp, "_check_all_persona_target_lists_fresh"),
            _patch.object(wp, "_load_in_list_canonical_urls", return_value=set()),
            _patch.object(wp, "_launch_and_download", side_effect=deltas),
        ]
        if process_side_effect is not None:
            stack.append(
                _patch.object(wp, "_process_prospects",
                              side_effect=process_side_effect)
            )
        for ctx in stack:
            ctx.start()
        try:
            return wp.run_weekly_prospecting(
                self._crm(), MagicMock(), search_export_id="a", dry_run=False
            )
        finally:
            for ctx in reversed(stack):
                ctx.stop()

    def _delta(self, csv_text=None):
        from tests.conftest import scrape_delta_from_csv

        return scrape_delta_from_csv(
            csv_text or self.CSV, csv_name="wk-operations-leaders-mx"
        )

    def test_advance_happens_on_success(self, monkeypatch, tmp_path):
        self._run(monkeypatch, tmp_path, deltas=[self._delta()])
        state = read_cursor_state("wk-operations-leaders-mx", sn_url=SN_URL)
        assert state.consumed_rows == 1
        # Both integrity anchors are stamped by the caller's advance.
        assert state.url_changed is False
        assert state.last_row_url == "https://www.linkedin.com/in/loop-cursor"

    def test_cursor_untouched_when_processing_raises(self, monkeypatch, tmp_path):
        """The whole point of moving the advance out of the downloader: a
        raise anywhere in the ingest must leave the rows re-servable next
        week rather than silently consumed."""
        summary = self._run(
            monkeypatch, tmp_path,
            deltas=[self._delta()],
            process_side_effect=RuntimeError("CRM exploded mid-scoring"),
        )
        assert summary["searches_aborted"] == 1
        assert read_cursor("wk-operations-leaders-mx") == 0

    def test_cursor_corruption_aborts_the_whole_run(self, monkeypatch, tmp_path):
        """A cursor state file we cannot trust is not a per-search hiccup:
        every remaining search reads the same file, and continuing would
        degrade each of them to a full re-ingest. It must propagate out of
        the per-search broad except, exactly like a 401."""
        with pytest.raises(CursorStateCorruptError, match="inspect it"):
            self._run(
                monkeypatch, tmp_path,
                deltas=[CursorStateCorruptError("… inspect it before re-running")],
            )

    def test_zero_delta_and_reset_counters_reach_the_summary(
        self, monkeypatch, tmp_path
    ):
        from workflows import weekly_prospect as wp

        zero = wp.ScrapeDelta(
            wp.ScrapeStatus.ZERO_DELTA, [], "wk-operations-leaders-mx", 300,
            cursor_reset=True,
        )
        summary = self._run(monkeypatch, tmp_path, deltas=[zero])
        assert summary["searches_zero_delta"] == 1
        assert summary["cursor_resets"] == 1

    def test_all_zero_delta_fires_a_loud_alarm(self, monkeypatch, tmp_path, capsys):
        """Independent of `qualified`: a run that ingested literally nothing
        would otherwise print a tidy row of zeros and no alarm at all."""
        from workflows import weekly_prospect as wp

        zero = wp.ScrapeDelta(
            wp.ScrapeStatus.ZERO_DELTA, [], "wk-operations-leaders-mx", 300
        )
        self._run(monkeypatch, tmp_path, deltas=[zero])
        err = capsys.readouterr().err
        assert "ZERO-DELTA ALARM" in err
        assert "all 1 search(es) returned no new rows" in err

    def test_partial_zero_delta_does_not_fire_the_alarm(
        self, monkeypatch, tmp_path, capsys
    ):
        from workflows import weekly_prospect as wp

        zero = wp.ScrapeDelta(
            wp.ScrapeStatus.ZERO_DELTA, [], "wk-operations-leaders-mx", 300
        )
        summary = self._run(
            monkeypatch, tmp_path,
            deltas=[zero, self._delta()],
            personas=self._personas({"mx": SN_URL, "br": "https://sn/2"}),
        )
        assert summary["searches_zero_delta"] == 1
        assert "ZERO-DELTA ALARM" not in capsys.readouterr().err

    def test_csv_name_collision_fails_loud_at_run_start(self, monkeypatch, tmp_path):
        """Two searches sharing a csvName share one PB file AND one cursor —
        each would eat the other's rows and both would look drained."""
        from workflows import weekly_prospect as wp

        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        monkeypatch.setattr(wp, "EXPORTS_DIR", tmp_path)
        with _patch.object(wp, "build_anthropic_client", return_value=None), \
             _patch.object(wp, "load_personas", return_value={}), \
             _patch.object(wp, "_check_all_persona_target_lists_fresh"), \
             _patch.object(wp, "_get_all_searches", return_value=[
                 ("ops_leaders", "mx", "https://sn/1"),
                 ("ops-leaders", "mx", "https://sn/2"),
             ]), \
             pytest.raises(RuntimeError, match="csvName collision"):
            wp.run_weekly_prospecting(
                MagicMock(), MagicMock(), search_export_id="a", dry_run=False
            )
