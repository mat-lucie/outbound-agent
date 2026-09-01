"""Tests for scripts/gmail_sweep.py — the 90-day Gmail conversation sweep.

The sender filter is a structural silencer: anything it matches vanishes from
the owed/waiting radar with no trace, so the tests below pin both directions —
real corporate counterparties survive, genuine bulk senders are dropped.

Regression of record: a bare ``@mail\\.`` alternative matched an address at
``mail.<brand>`` — a live counterparty's REAL corporate domain on a brand
gTLD — making the account invisible to every sweep run. Note that requiring
one more label (``mail.<brand>.<tld>``) does NOT fix the class:
``mail.<brand>.com.br`` is a corporate mail subdomain with the same shape as
``mail.sendgrid.net``. Bulk senders are recognised by their ESP's registrable
domain instead, and the survivor table below is what keeps that honest.

All identities here are synthetic (Acme and friends); the SHAPES they encode
— ESP domain, freemail host, human at a vendor domain, ``mail.<brand>``
subdomain — are the real thing.
"""
from __future__ import annotations

import pytest

from scripts.gmail_sweep import (
    MAX_PAGES,
    external_addresses,
    fetch_threads,
    group_counterparties,
    internal_domains,
    is_automated,
    is_internal,
    list_all,
)

# A human at a corporate domain whose registrable domain IS a brand gTLD —
# `mail.northwind` with no further label. This is the address the old
# `@mail\.` rule silenced.
BRAND_GTLD = "s.rivera@mail.northwind"

# Deliberately NOT an example.* domain: `internal_domains` treats the
# IANA-reserved documentation domains as unconfigured placeholders, so an
# example.com operator would make this whole fixture a no-op.
OPERATOR_DOMAIN = "operatorco.com"


@pytest.fixture(autouse=True)
def _operator_identity(monkeypatch):
    """Declare the operator's own domain — the sweep is unconfigured without
    it (see `internal_domains`), and every test below needs sent-vs-received
    to be decidable."""
    monkeypatch.setenv("OUTBOUND_INTERNAL_DOMAINS", OPERATOR_DOMAIN)
    monkeypatch.delenv("OUTBOUND_SWEEP_VENDOR_DOMAINS", raising=False)


def test_brand_gtld_corporate_domain_survives_the_filter():
    """The bug: mail.<brand> is a corporate domain, not a bulk-mail subdomain."""
    assert not is_automated(BRAND_GTLD)


def test_genuine_bulk_sender_is_still_filtered():
    assert is_automated("hello@mail.sendgrid.net")


@pytest.mark.parametrize("address", [
    BRAND_GTLD,
    # Same shape as mail.sendgrid.net — a label-counting rule silences these,
    # which is why the filter matches ESP registrable domains instead.
    "s.rivera@mail.northwind.com",
    "contato@mail.acmesteel.com.br",
    "j.silva@email.acmemining.com",
    "ventas@mail.acmecement.com",
    "t.suzuki@mail.acmemotors.co.jp",
    "jorge@team.zone",           # real-company-shaped domain, generic first label
    "hola@send.example",         # two labels only, bulk-sounding first label
    "sales@notifyacme.com",      # brand that merely starts with "notify"
    "contacto@azurewind.com",    # brand that merely contains "azure"
    "cuentas@invoicehub.com",    # brand that merely contains "invoice"
    "it.support@acmesteel.com",  # role word inside a human's local part
    "compras.invoices@acmefoundry.com.br",
    "soporte@acmeequipment.com",  # ES/PT role mailboxes are never silenced
    "facturacion@acmesteel.com.mx",
    "ventas@acmesteel.com.br",
    # LinkedIn runs the outreach program: its robot mailboxes are silenced,
    # a human writing about an account restriction must still reach the radar.
    "recruiter.maria@linkedin.com",
])
def test_real_counterparties_are_not_silenced(address):
    assert not is_automated(address)


@pytest.mark.parametrize("address", [
    "hello@mail.sendgrid.net",
    "bounces-9@mcsv.net",
    "campaign@em.mailchimp.com",
    "digest@substack.com",
    "root@sns.amazonaws.com",
    "no-reply@anything.com",
    "noreply@anything.com",
    "x-noreply@anything.com",
    "notifications@somepublisher.com",
    "calendar-notification@google.com",
    "invitations@linkedin.com",
    "mailer-daemon@googlemail.com",
    "support@acmeequipment.com",   # bare role mailbox
    "billing@somevendor.io",
    "info@linkedin.com",
])
def test_automated_senders_are_filtered(address):
    assert is_automated(address)


def test_internal_addresses_are_recognised():
    assert is_internal(f"dana@{OPERATOR_DOMAIN}")
    assert not is_internal(BRAND_GTLD)


# ---------------------------------------------------------------------------
# Operator identity is CONFIG, not a constant (fork seam)
# ---------------------------------------------------------------------------

def test_internal_domains_reads_the_explicit_env_list(monkeypatch):
    monkeypatch.setenv("OUTBOUND_INTERNAL_DOMAINS", "acme.com, @acme.io ")
    assert internal_domains() == frozenset({"acme.com", "acme.io"})
    assert is_internal("dana@ACME.IO")
    assert is_internal("dana@mail.acme.com")   # subdomains of ours are ours
    assert not is_internal("dana@notacme.com")


def test_internal_domains_falls_back_to_the_campaign_identity(monkeypatch):
    monkeypatch.delenv("OUTBOUND_INTERNAL_DOMAINS", raising=False)
    monkeypatch.delenv("EMAIL_REPLY_TO", raising=False)
    monkeypatch.setenv("EMAIL_FROM", "Acme Outbound <hi@acme.com>")
    assert internal_domains() == frozenset({"acme.com"})


def test_internal_domains_is_empty_when_nothing_is_configured(monkeypatch):
    """Empty means UNCONFIGURED — main() refuses to run rather than report
    every thread as owed because no message can be recognised as ours."""
    for var in ("OUTBOUND_INTERNAL_DOMAINS", "EMAIL_FROM", "EMAIL_REPLY_TO"):
        monkeypatch.delenv(var, raising=False)
    assert internal_domains() == frozenset()
    assert not is_internal("dana@example.com")


def test_shipped_example_identity_reads_as_unconfigured(monkeypatch):
    """`.env.example` ships EMAIL_FROM/EMAIL_REPLY_TO as outbound@example.com,
    UNCOMMENTED. An operator who copied it without editing must still trip the
    fail-loud UNCONFIGURED guard — otherwise example.com resolves as "ours"
    and every thread misclassifies silently, which is the exact failure the
    guard exists to prevent."""
    monkeypatch.delenv("OUTBOUND_INTERNAL_DOMAINS", raising=False)
    monkeypatch.setenv("EMAIL_FROM", "Outbound Agent <outbound@example.com>")
    monkeypatch.setenv("EMAIL_REPLY_TO", "outbound@example.com")
    assert internal_domains() == frozenset()


def test_example_domains_are_dropped_from_the_explicit_env_list(monkeypatch):
    """Same rule on the declared path: a reserved domain is a placeholder,
    never an operator identity, however it arrived."""
    monkeypatch.setenv(
        "OUTBOUND_INTERNAL_DOMAINS",
        "example.com, example.org, example.net, mail.example.com, corp.example, acme.com",
    )
    assert internal_domains() == frozenset({"acme.com"})


def test_env_list_of_only_example_domains_reads_as_unconfigured(monkeypatch):
    monkeypatch.setenv("OUTBOUND_INTERNAL_DOMAINS", "example.com, sub.example.net")
    # Must NOT silently fall through to the EMAIL_FROM fallback either.
    monkeypatch.setenv("EMAIL_FROM", "Outbound Agent <outbound@example.com>")
    assert internal_domains() == frozenset()


def test_extra_vendor_domains_come_from_config(monkeypatch):
    assert not is_automated("newsroom@somevendor.example")
    monkeypatch.setenv("OUTBOUND_SWEEP_VENDOR_DOMAINS", "somevendor.example")
    assert is_automated("newsroom@somevendor.example")
    # Suffix match, not substring: a lookalike brand still reaches the radar.
    assert not is_automated("ventas@notsomevendor.example")


def _msg(from_addr, to_addr):
    return {"payload": {"headers": [
        {"name": "From", "value": from_addr},
        {"name": "To", "value": to_addr},
    ]}}


def test_thread_with_a_brand_gtld_yields_an_external_counterparty():
    """End-to-end guard: the thread must not collapse to zero participants.

    Zero external participants makes the sweep skip the thread outright —
    that is how the silenced account stayed off the radar.
    """
    thread = [
        _msg(f"Dana <dana@{OPERATOR_DOMAIN}>", f"Sofia <{BRAND_GTLD}>"),
        _msg(f"Sofia <{BRAND_GTLD}>", f"Dana <dana@{OPERATOR_DOMAIN}>"),
    ]
    assert external_addresses(thread) == {BRAND_GTLD}


def test_thread_with_only_robots_yields_nothing():
    thread = [_msg("no-reply@calendar.google.com", f"dana@{OPERATOR_DOMAIN}")]
    assert external_addresses(thread) == set()


# ---------------------------------------------------------------------------
# Grouping + direction-of-ball
#
# Regression of record (a live run): six counterparties were reported as "we
# owe a reply" (ball_mine=true) although the newest sent/received message on
# the live thread was FROM us. Two mechanisms, both pinned below:
#
#   1. SPLIT KEYS: a thread keyed by its single external domain when unique,
#      else by its alphabetically-first address. One person's live thread
#      (which CC'd a second company) landed under an address key while their
#      stale calendar-invite thread landed under the domain key — the domain
#      row then reported "owed since <weeks ago>" for a fully-answered
#      counterparty.
#   2. AUTO-RESPONSES SET DIRECTION: a calendar RSVP ("Accepted: …") or an
#      out-of-office reply arriving after our real reply flipped the group
#      back to "we owe a reply". An RSVP is not a message anyone answers.
# ---------------------------------------------------------------------------

OPERATOR = f"Dana Ortiz <dana@{OPERATOR_DOMAIN}>"
NOELIA = "Noelia Prado <noelia.prado@acmebeverage.com>"
RAMIRO = "Ramiro Souza <ramiro@acmelabs.com>"
TOMAS = "Tomas Solis <tomas.solis@acme-foods.com>"


def _tmsg(from_addr, to_addr, *, ms, subject="Re: Acme", labels=("INBOX",),
          auto_submitted=None):
    headers = [
        {"name": "From", "value": from_addr},
        {"name": "To", "value": to_addr},
        {"name": "Subject", "value": subject},
    ]
    if auto_submitted:
        headers.append({"name": "Auto-Submitted", "value": auto_submitted})
    return {"payload": {"headers": headers},
            "labelIds": list(labels), "internalDate": str(ms)}


def _thread(*messages):
    return {"messages": list(messages)}


def _rows_by_key(threads):
    rows = group_counterparties(threads)
    return {r["counterparty"]: r for r in rows}


def test_answered_counterparty_is_not_split_into_an_owed_domain_row():
    """The live thread CC'd a colleague at a second company, so it keyed by
    address, while a stale single-message calendar-invite thread keyed by
    domain — and the domain row reported us as owing a reply from weeks
    back. All threads naming a counterparty must land in ONE group, and a
    group whose newest human message is a SENT one is never "we owe"."""
    threads = {
        "invite": _thread(
            _tmsg(NOELIA, OPERATOR, ms=100, subject="Catch-up Dana <> Noelia"),
        ),
        "live": _thread(
            _tmsg(NOELIA, OPERATOR, ms=50),
            _tmsg(OPERATOR, f"{NOELIA}, {RAMIRO}", ms=200, labels=("SENT",)),
        ),
    }
    rows = _rows_by_key(threads)
    beverage = rows["acmebeverage.com"]
    assert beverage["ball_mine"] is False
    assert beverage["n_threads"] == 2
    assert beverage["latest_ms"] == 200
    # No second row competing for the same person under an address key.
    assert not any("acmebeverage" in k for k in rows if k != "acmebeverage.com")


def test_calendar_rsvp_does_not_flip_the_ball_back_to_us():
    """We replied last on the live thread at ms=200; the counterparty's
    calendar acceptance ("Accepted: …" — no Sender or Auto-Submitted header,
    only the subject marks it) arrived later in its own thread and flipped
    the whole domain group to "we owe a reply"."""
    threads = {
        "live": _thread(
            _tmsg(OPERATOR, TOMAS, ms=100, labels=("SENT",)),
            _tmsg('"Rivas, Jose Ignacio" <Jose.Rivas@acme-foods.com>',
                  OPERATOR, ms=150),
            _tmsg(OPERATOR, TOMAS, ms=200, labels=("SENT",)),
        ),
        "rsvp": _thread(
            _tmsg(TOMAS, OPERATOR, ms=300,
                  subject="Accepted: Acme / Northwind - Presentation"),
        ),
    }
    row = _rows_by_key(threads)["acme-foods.com"]
    assert row["ball_mine"] is False
    assert row["latest_ms"] == 200


def test_out_of_office_reply_does_not_count_as_their_reply():
    """An OOO (Auto-Submitted: auto-generated) after our send must leave
    the thread as waiting-on-them, not as an inbound reply we owe."""
    threads = {
        "t": _thread(
            _tmsg(OPERATOR, NOELIA, ms=100, labels=("SENT",)),
            _tmsg(NOELIA, OPERATOR, ms=150,
                  subject="Respuesta automática: Reconectando",
                  auto_submitted="auto-generated"),
        ),
    }
    row = _rows_by_key(threads)["acmebeverage.com"]
    assert row["ball_mine"] is False
    assert row["latest_ms"] == 100


def test_group_with_only_auto_responses_still_surfaces_as_owed():
    """A bare unanswered calendar invite is still worth a row — the
    auto-response demotion is a fallback, not a silencer."""
    threads = {
        "invite": _thread(
            _tmsg(NOELIA, OPERATOR, ms=100,
                  subject="Accepted: Catch-up Dana <> Noelia"),
        ),
    }
    row = _rows_by_key(threads)["acmebeverage.com"]
    assert row["ball_mine"] is True
    assert row["latest_ms"] == 100


def test_freemail_senders_are_not_collapsed_into_one_domain_row():
    """A live sweep rendered 25 threads from 25 unrelated people as a single
    'gmail.com' counterparty."""
    threads = {
        "a": _thread(_tmsg("Ana <ana.consultant@gmail.com>", OPERATOR, ms=100)),
        "b": _thread(_tmsg("Beto <beto.planta@gmail.com>", OPERATOR, ms=200)),
    }
    rows = _rows_by_key(threads)
    assert "gmail.com" not in rows
    assert rows["ana.consultant@gmail.com"]["ball_mine"] is True
    assert rows["beto.planta@gmail.com"]["ball_mine"] is True


def test_multi_company_thread_reaches_both_counterparties():
    threads = {
        "t": _thread(
            _tmsg(TOMAS, f"{OPERATOR}, h.lara@acmedairy.com", ms=100),
        ),
    }
    rows = _rows_by_key(threads)
    assert rows["acme-foods.com"]["ball_mine"] is True
    assert rows["acmedairy.com"]["ball_mine"] is True
    assert rows["acme-foods.com"]["addresses"] == ["tomas.solis@acme-foods.com"]
    assert rows["acmedairy.com"]["addresses"] == ["h.lara@acmedairy.com"]


def test_drafts_still_do_not_set_direction():
    threads = {
        "t": _thread(
            _tmsg(NOELIA, OPERATOR, ms=100),
            _tmsg(OPERATOR, NOELIA, ms=200, labels=("DRAFT",)),
        ),
    }
    row = _rows_by_key(threads)["acmebeverage.com"]
    assert row["ball_mine"] is True
    assert row["latest_ms"] == 100


# ---------------------------------------------------------------------------
# Degradation: a lost page or thread must be REPORTED, never silently dropped
# ---------------------------------------------------------------------------

class _FakeList:
    def __init__(self, pages):
        self._pages = pages
        self.calls = 0

    def list(self, **kwargs):
        self.calls += 1
        page = self._pages[min(self.calls - 1, len(self._pages) - 1)]
        return _FakeExec(page)


class _FakeExec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeUsers:
    def __init__(self, messages=None, threads=None):
        self._messages = messages
        self._threads = threads

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads


class _FakeSvc:
    def __init__(self, messages=None, threads=None):
        self._users = _FakeUsers(messages, threads)

    def users(self):
        return self._users


def test_list_all_reports_complete_when_pagination_exhausts():
    svc = _FakeSvc(messages=_FakeList([
        {"messages": [{"threadId": "a"}], "nextPageToken": "t1"},
        {"messages": [{"threadId": "b"}]},
    ]))
    msgs, pages, complete = list_all(svc, "q")
    assert [m["threadId"] for m in msgs] == ["a", "b"]
    assert (pages, complete) == (2, True)


def test_list_all_returns_partial_instead_of_dying_on_api_error():
    """The contract requires 'sweep incomplete — first N threads only'.

    An exception escaping the loop would print no JSON at all, making that
    documented degradation path unreachable.
    """
    svc = _FakeSvc(messages=_FakeList([
        {"messages": [{"threadId": "a"}], "nextPageToken": "t1"},
        RuntimeError("429 rate limited"),
    ]))
    msgs, pages, complete = list_all(svc, "q")
    assert [m["threadId"] for m in msgs] == ["a"]
    assert (pages, complete) == (1, False)


def test_list_all_breaks_out_of_a_repeated_page_token():
    """A stable nextPageToken would otherwise spin forever and hang the run."""
    svc = _FakeSvc(messages=_FakeList([
        {"messages": [{"threadId": "a"}], "nextPageToken": "same"},
    ]))
    msgs, pages, complete = list_all(svc, "q")
    assert complete is False
    assert pages < MAX_PAGES


def test_list_all_flags_the_page_cap_instead_of_truncating_silently():
    # A distinct token on every page: only MAX_PAGES stops this.
    class _Endless(_FakeList):
        def list(self, **kwargs):
            self.calls += 1
            return _FakeExec({"messages": [{"threadId": str(self.calls)}],
                              "nextPageToken": f"t{self.calls}"})

    svc = _FakeSvc(messages=_Endless([{}]))
    msgs, pages, complete = list_all(svc, "q")
    assert (len(msgs), pages, complete) == (MAX_PAGES, MAX_PAGES, False)


def test_fetch_threads_names_the_threads_it_lost():
    class _Threads:
        def get(self, **kwargs):
            if kwargs["id"] == "bad":
                return _FakeExec(RuntimeError("500"))
            return _FakeExec({"messages": [{"id": kwargs["id"]}]})

    threads, failed = fetch_threads(_FakeSvc(threads=_Threads()), ["ok", "bad"])
    assert set(threads) == {"ok"}
    assert failed == ["bad"]
