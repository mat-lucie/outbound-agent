"""Typed exceptions for the ``CRMProvider`` contract.

The contract's *normalized exception family*: vendor-neutral error types that
adapters raise so callers handle a failure mode by its *meaning* rather than by
inspecting a vendor's HTTP status / body. This mirrors the read-side
normalization (``Record`` / ``Entry`` instead of raw vendor JSON) on the error
side — a caller catches :class:`UniquenessConflictError`, never an
``httpx.HTTPStatusError`` it has to sniff for "is this a 400 uniqueness body?".

Scope. Only the failure modes the engine actually branches on belong here.
Today that is exactly one: a vendor uniqueness-constraint violation on a write
(the cross-machine concurrent-run guard in ``workflows/daily_run.py`` depends on
distinguishing "someone else holds this key" from any other 4xx). The base
:class:`CRMError` exists so future neutral error types share a catchable root
and so callers can ``except CRMError`` as a catch-all for contract-level
failures; it is intentionally NOT a catch-all for transport errors, which still
propagate as the adapter's underlying exception type.
"""

from __future__ import annotations


class CRMError(Exception):
    """Base class for the ``CRMProvider`` contract's normalized exceptions.

    Adapters raise subclasses of this for failure modes the contract defines a
    neutral meaning for. Transport / connectivity errors are NOT wrapped in
    ``CRMError`` — they propagate as the adapter's native exception so a caller
    that needs transport-level handling still sees it.
    """


class ResultTruncatedError(CRMError):
    """Raised by a read (:meth:`CRMProvider.query_list_entries`) that hit its
    ``limit`` with records still remaining, when the caller passed
    ``fail_if_truncated=True``.

    The neutral signal for "a full-sweep read was silently incomplete". Full-
    sweep callers (e.g. the suppression sweep, list-scan exports) prefer a loud
    failure over an incomplete result set — a silently truncated sweep would
    leak suppressed prospects into outbound or export a partial pipeline
    (PR-234). The Attio adapter maps its vendor-native ``AttioResultTruncated``
    to this type; the in-memory ``FakeProvider`` raises it directly. Callers
    catch THIS rather than a vendor-specific truncation exception.
    """


class UniquenessConflictError(CRMError):
    """Raised by a write (:meth:`CRMProvider.create_object_record` /
    :meth:`CRMProvider.update_object_record`) when the vendor rejects it for
    violating a uniqueness constraint on one of the written attributes.

    The neutral signal for "a record with this unique value already exists".
    The Attio adapter maps its vendor-native uniqueness violation (an HTTP 400 /
    409 whose body names a uniqueness/duplicate/already-exists condition) to
    this type; the in-memory ``FakeProvider`` raises it when a write would
    duplicate a value on an attribute the test registered as unique. Callers
    (e.g. ``daily_run.open_daily_run`` / ``attach_daily_run``) catch THIS rather
    than sniffing a vendor HTTP body, so the concurrent-run guard is
    vendor-neutral.

    ``object_type`` and ``attribute`` name the offending object slug and (when
    the adapter can determine it) the unique attribute; ``attribute`` is
    ``None`` when the vendor body does not identify the specific field.
    """

    def __init__(
        self,
        object_type: str,
        *,
        attribute: str | None = None,
        message: str | None = None,
    ) -> None:
        self.object_type = object_type
        self.attribute = attribute
        detail = message or (
            f"a record with this value already exists on {object_type!r}"
            + (f" (unique attribute {attribute!r})" if attribute else "")
        )
        super().__init__(detail)
