"""CRM field/stage mapping — the config-driven translation layer at the seam.

The outbound engine carries its own canonical vocabulary: engine field names
(``linkedin_url``, ``full_name``, …) and canonical pipeline stages (the VALUES
of :class:`~models.pipeline.PipelineStage`, e.g. ``"Connection Sent"``). A
concrete CRM, however, names its fields with vendor slugs and labels its
pipeline-stage options however the operator configured them. :class:`CRMMapping`
is the frozen translation table that lets an adapter bridge the two, driven by
``config/crm.yaml``'s ``field_mapping`` / ``stage_field`` / ``stage_mapping``
sections.

Scope of what is CONSUMED today (Commit 1 of the wiring increment):
    * Only the **stage label + rank** translation is wired through the Attio
      adapter (read: vendor label → canonical stage value + rank; write:
      canonical stage value → vendor label). ``field_mapping`` and
      ``stage_field`` are LOADED and validated here, and exposed via
      :meth:`CRMMapping.field_slug`, but their CONSUMPTION at the adapter
      boundary lands in Commit 2 (field-slug + stage-field-slug plumbing).

Behavior-preservation invariant. For Attio the vendor option label already
EQUALS the canonical :class:`PipelineStage` value (the live list's stage options
are literally "Connection Sent", "DM1 Sent", …). So :func:`default_attio_mapping`
builds the **identity** mapping over every stage, and the stage_rank table is a
straight copy of :data:`~models.pipeline.STAGE_RANK` keyed by value. Under that
default every translation method is a passthrough and the adapter's observable
behavior is byte-identical to the pre-mapping code.

Fail-loud. :func:`load_crm_mapping` reads ``config/crm.yaml`` LIVE on every call
(it never caches at import — mirroring :mod:`clients.settings`, which reads
``os.environ`` live) and validates every present section, raising
:class:`~clients.settings.ConfigError` on any malformed shape rather than
silently falling back to a default that would mask the operator's typo. A
section that is *absent* falls back to its default; a section that is *present
but malformed* raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clients.settings import ConfigError, config_dir, load_yaml
from models.pipeline import STAGE_RANK, PipelineStage


@dataclass(frozen=True)
class CRMMapping:
    """Immutable field + stage translation table for one CRM vendor.

    Attributes:
        field_mapping: engine field name -> vendor field slug (e.g.
            ``{"linkedin_url": "linkedin"}``). LOADED + validated this commit;
            CONSUMED in Commit 2 via :meth:`field_slug`.
        stage_field: the vendor field slug that carries the pipeline stage
            (default ``"stage"``). LOADED this commit; CONSUMED in Commit 2.
        canonical_to_vendor_stage: canonical :class:`PipelineStage` VALUE ->
            vendor option label. Covers EVERY engine stage (a partial config is
            merged with the identity default — see :func:`load_crm_mapping`).
        vendor_to_canonical_stage: the inverse of ``canonical_to_vendor_stage``.
            Built so the reverse lookup is unambiguous (the loader rejects two
            engine stages sharing a vendor label).
        stage_rank: canonical :class:`PipelineStage` VALUE -> funnel rank.
    """

    field_mapping: dict[str, str]
    stage_field: str
    canonical_to_vendor_stage: dict[str, str]
    vendor_to_canonical_stage: dict[str, str]
    stage_rank: dict[str, int]

    def field_slug(self, engine_field: str) -> str:
        """Return the vendor slug for ``engine_field``.

        Unmapped fields pass through unchanged — an engine field with no
        explicit mapping is assumed to share its name with the vendor slug.
        (Consumed in Commit 2.)
        """
        return self.field_mapping.get(engine_field, engine_field)

    def to_vendor_stage(self, canonical: str) -> str:
        """Translate a canonical stage value to the vendor's option label.

        An unknown canonical string passes through unchanged (the adapter never
        invents a label it wasn't given). Under the identity default this is a
        no-op for every known stage.
        """
        return self.canonical_to_vendor_stage.get(canonical, canonical)

    def to_canonical_stage(self, vendor_label: str) -> str:
        """Translate a vendor option label back to the canonical stage value.

        An unrecognized label passes through unchanged so the adapter surfaces
        the raw label (with ``rank=None``) rather than crashing — mirroring the
        pre-mapping "unknown stage → name kept, rank None" behavior.
        """
        return self.vendor_to_canonical_stage.get(vendor_label, vendor_label)

    def rank_for_canonical(self, canonical: str) -> int | None:
        """Return the funnel rank for a canonical stage value, or ``None`` if
        the value is not a known stage (the unknown-stage / empty-string case
        the adapter must tolerate)."""
        return self.stage_rank.get(canonical)


def default_attio_mapping() -> CRMMapping:
    """Build the identity mapping that reproduces today's Attio behavior exactly.

    field_mapping is the documented Attio default; stage_field is ``"stage"``;
    the stage maps are the IDENTITY over every :class:`PipelineStage`
    (``{s.value: s.value}``), and stage_rank is :data:`STAGE_RANK` re-keyed by
    value. Every :meth:`CRMMapping.to_vendor_stage` /
    :meth:`CRMMapping.to_canonical_stage` / :meth:`CRMMapping.rank_for_canonical`
    call under this mapping returns exactly what the pre-mapping code computed.
    """
    identity = {s.value: s.value for s in PipelineStage}
    return CRMMapping(
        field_mapping={
            "linkedin_url": "linkedin",
            "full_name": "name",
            "company_domain": "domains",
        },
        stage_field="stage",
        canonical_to_vendor_stage=dict(identity),
        vendor_to_canonical_stage=dict(identity),
        stage_rank={s.value: STAGE_RANK[s] for s in PipelineStage},
    )


def _validate_field_mapping(raw: Any) -> dict[str, str]:
    """Validate the ``field_mapping`` section: a mapping of str -> str."""
    if not isinstance(raw, dict):
        raise ConfigError(
            "config/crm.yaml 'field_mapping' must be a mapping of "
            f"engine-field-name -> vendor-slug, got {type(raw).__name__}."
        )
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError(
                "config/crm.yaml 'field_mapping' entries must be string -> "
                f"string, got {key!r} -> {value!r}."
            )
        out[key] = value
    return out


def _validate_stage_field(raw: Any) -> str:
    """Validate the ``stage_field`` section: a non-empty string."""
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(
            "config/crm.yaml 'stage_field' must be a non-empty string naming "
            f"the vendor field that carries the stage, got {raw!r}."
        )
    return raw


def _build_stage_maps(
    raw_stage_mapping: Any,
) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """Validate ``stage_mapping`` and merge it with the identity default.

    The config keys are :class:`PipelineStage` NAMES (e.g. ``"CONNECTION_SENT"``,
    NOT the value ``"Connection Sent"``). Each entry is a mapping with a
    non-empty string ``name`` (the vendor label) and an int ``rank``.

    Merge behavior (so a PARTIAL config still covers every engine stage): start
    from the identity mapping + :data:`STAGE_RANK` for ALL stages, then override
    only the stages the config names. A member absent from the config keeps its
    identity label + canonical rank — never dropped.

    Raises :class:`ConfigError` (fail-loud) on: a non-mapping section, an unknown
    PipelineStage name, a non-mapping entry, a missing / empty / non-string
    ``name``, a missing / non-int ``rank``, or two engine stages mapping to the
    SAME vendor label (which would make the reverse lookup ambiguous).
    """
    if not isinstance(raw_stage_mapping, dict):
        raise ConfigError(
            "config/crm.yaml 'stage_mapping' must be a mapping of "
            f"PipelineStage-name -> {{name, rank}}, got "
            f"{type(raw_stage_mapping).__name__}."
        )

    # Start from the identity default for every engine stage so a partial
    # config still covers the full set.
    canonical_to_vendor: dict[str, str] = {s.value: s.value for s in PipelineStage}
    stage_rank: dict[str, int] = {s.value: STAGE_RANK[s] for s in PipelineStage}

    valid_names = {s.name for s in PipelineStage}
    for key, entry in raw_stage_mapping.items():
        if key not in valid_names:
            raise ConfigError(
                f"config/crm.yaml 'stage_mapping' has unknown PipelineStage "
                f"name {key!r}. Keys must be PipelineStage NAMES (e.g. "
                f"'CONNECTION_SENT'), one of: {sorted(valid_names)}."
            )
        if not isinstance(entry, dict):
            raise ConfigError(
                f"config/crm.yaml 'stage_mapping[{key}]' must be a mapping with "
                f"'name' and 'rank', got {type(entry).__name__}."
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                f"config/crm.yaml 'stage_mapping[{key}].name' must be a "
                f"non-empty string (the vendor option label), got {name!r}."
            )
        rank = entry.get("rank")
        # bool is a subclass of int — reject it explicitly so a YAML `true`
        # isn't silently accepted as rank 1. Negative ranks are rejected too:
        # rank is a funnel position (higher = further along), so a negative
        # value is always a misconfiguration that would silently invert ordering
        # for any consumer that trusts mapping.stage_rank.
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 0:
            raise ConfigError(
                f"config/crm.yaml 'stage_mapping[{key}].rank' must be a "
                f"non-negative integer funnel rank, got {rank!r}."
            )
        canonical = PipelineStage[key].value
        canonical_to_vendor[canonical] = name
        stage_rank[canonical] = rank

    # Build the inverse, rejecting ambiguous reverse lookups (two engine stages
    # mapping to the same vendor label).
    vendor_to_canonical: dict[str, str] = {}
    for canonical, vendor_label in canonical_to_vendor.items():
        if vendor_label in vendor_to_canonical:
            raise ConfigError(
                "config/crm.yaml 'stage_mapping' maps two engine stages to the "
                f"same vendor label {vendor_label!r} "
                f"({vendor_to_canonical[vendor_label]!r} and {canonical!r}); "
                "the reverse lookup would be ambiguous. Give each engine stage "
                "a distinct vendor label."
            )
        vendor_to_canonical[vendor_label] = canonical

    return canonical_to_vendor, vendor_to_canonical, stage_rank


def load_crm_mapping() -> CRMMapping:
    """Load the CRM mapping from ``config/crm.yaml`` (LIVE — never cached).

    Resolution:
        * ``config/crm.yaml`` ABSENT → :func:`default_attio_mapping` (the
          backward-compatible identity default; behavior byte-identical to the
          pre-mapping engine).
        * ``config/crm.yaml`` PRESENT → read its ``field_mapping`` /
          ``stage_field`` / ``stage_mapping`` sections. Any section that is
          ABSENT falls back to that section's default; any section that is
          PRESENT but malformed raises :class:`ConfigError` (fail-loud, never a
          silent default that masks a typo).

    A partial ``stage_mapping`` (only some stages named) still yields a complete
    mapping: the named stages are remapped and every other engine stage keeps
    its identity label + canonical rank (see :func:`_build_stage_maps`).
    """
    config_path = config_dir() / "crm.yaml"
    if not config_path.exists():
        return default_attio_mapping()

    config = load_yaml("crm")
    default = default_attio_mapping()

    # A partial field_mapping MERGES over the canonical default — naming only
    # `linkedin_url` must NOT drop the default `full_name`/`company_domain`
    # slugs (that would make `_field_slug("full_name")` fall back to the engine
    # name "full_name", which is not a valid vendor slug → silently blank reads).
    # This mirrors _build_stage_maps' partial-stage_mapping merge so the two
    # partial-config behaviors are consistent.
    field_mapping = (
        {**default.field_mapping, **_validate_field_mapping(config["field_mapping"])}
        if "field_mapping" in config
        else default.field_mapping
    )
    stage_field = (
        _validate_stage_field(config["stage_field"])
        if "stage_field" in config
        else default.stage_field
    )

    if "stage_mapping" in config:
        canonical_to_vendor, vendor_to_canonical, stage_rank = _build_stage_maps(
            config["stage_mapping"]
        )
    else:
        canonical_to_vendor = default.canonical_to_vendor_stage
        vendor_to_canonical = default.vendor_to_canonical_stage
        stage_rank = default.stage_rank

    return CRMMapping(
        field_mapping=field_mapping,
        stage_field=stage_field,
        canonical_to_vendor_stage=canonical_to_vendor,
        vendor_to_canonical_stage=vendor_to_canonical,
        stage_rank=stage_rank,
    )
