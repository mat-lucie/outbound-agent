"""Campaign definitions: personas, languages, message template loading.

# PR-16 (B-PD-005 + B-PD-008)

`get_message` previously fell back to Spanish copy when the requested
`(persona, language, step)` triple wasn't present in `messages.json`.
That silent fallback shipped Spanish DMs to English/Portuguese
prospects — a §0 #9 violation. PR-16 replaces the fallback with a
typed `MissingMessageError` that mirrors PR-12's `MissingLanguageError`
shape, plus a `variant` field for the PR-32+ weekly_brain proposals.

**Outbound-send callers** (`workflows/daily_check.py::run_connection_requests`
+ `run_dm_sequencing`) catch `MissingMessageError` and open a
`missing_copy` Operator Review Queue row so the operator can triage
the gap — no prospect silently receives a wrong-language message body.

**Classifier-context callers** (`workflows/detect_responses.py`) catch
`MissingMessageError` and fall back to a generic Spanish opener for
the response classifier ONLY. No outbound is sent from that path —
the per-prospect missing-copy event surfaces (if at all) at the
actual send path, where the queue row fires.

**Deferred raise sites** (will be added when their host modules land):
  - `workflows/hot_lead_alert.py::emit_hot_lead` (PR-18)
  - `workflows/weekly_brain.py::propose_variant` (PR-32+)
  - `workflows/weekly_prospect.py::_build_prospect_entry_attrs` preview
    (a follow-up that wires fail-fast at PROSPECT-commit time)
"""

import json
import os
from enum import Enum
from pathlib import Path

from models.enums import Language  # noqa: F401 — re-exported for backward compatibility


class MissingMessageError(Exception):
    """Raised by `get_message()` when the requested
    `(persona, language, dm_step, variant)` key returns no body.

    Mirrors PR-12's `MissingLanguageError` shape — structured fields
    surface every triage axis without parsing a message string. The
    `missing_copy` Operator Review Queue type carries the same fields
    via `MissingMessagePayload`.

    `variant` defaults to "default" since v1 messages.json doesn't
    expose explicit variants. The field is reserved for PR-32+ when
    weekly_brain proposes new variants for experiments.
    """

    def __init__(
        self,
        *,
        persona: str | None = None,
        language: str | None = None,
        dm_step: str | None = None,
        variant: str | None = None,
        record_id: str | None = None,
    ) -> None:
        self.persona = persona
        self.language = language
        self.dm_step = dm_step
        self.variant = variant
        self.record_id = record_id
        super().__init__(self._format())

    def _format(self) -> str:
        parts: list[str] = []
        if self.persona is not None:
            parts.append(f"persona={self.persona!r}")
        if self.language is not None:
            parts.append(f"language={self.language!r}")
        if self.dm_step is not None:
            parts.append(f"dm_step={self.dm_step!r}")
        if self.variant is not None:
            parts.append(f"variant={self.variant!r}")
        if self.record_id is not None:
            parts.append(f"record_id={self.record_id!r}")
        return "missing message copy: " + ", ".join(parts)


class Persona(Enum):
    DIGITALIZATION_CHAMPIONS = "digitalization_champions"
    OPERATIONS_LEADERS = "operations_leaders"
    EXECUTIVE_SPONSORS = "executive_sponsors"
    MX_MIDMARKET_MANUFACTURING = "mx_midmarket_manufacturing"
    CO_MIDMARKET_MANUFACTURING = "co_midmarket_manufacturing"
    CL_MIDMARKET_MANUFACTURING = "cl_midmarket_manufacturing"

    @classmethod
    def from_attio(cls, value: str) -> "Persona":
        try:
            return cls(value)
        except ValueError:
            return cls.OPERATIONS_LEADERS


class MessageStep(Enum):
    CONNECTION_NOTE = "connection_note"
    DM1 = "dm1"
    DM2 = "dm2"
    DM3 = "dm3"


# DM step number mapping (for Attio dm_step attribute)
DM_STEP_NUMBER: dict[MessageStep, int] = {
    MessageStep.CONNECTION_NOTE: 0,
    MessageStep.DM1: 1,
    MessageStep.DM2: 2,
    MessageStep.DM3: 3,
}

# Runtime content directory. Defaults to the repo-root `content/` (which ships
# NEUTRAL placeholder defaults). Operators repoint to their own filled-in content
# — or to the bundled `examples/acme/content/` reference — by exporting
# OUTBOUND_CONTENT_DIR. Absent the env var, behavior is identical to the prior
# hardcoded path.
CONTENT_DIR = Path(
    os.environ.get("OUTBOUND_CONTENT_DIR")
    or (Path(__file__).resolve().parent.parent / "content")
)


def load_messages() -> dict:
    """Load the 36 message variants from messages.json."""
    with open(CONTENT_DIR / "messages.json") as f:
        return json.load(f)


def load_personas() -> dict:
    """Load persona definitions from personas.json.

    Validates `search_size_credit` (the search-scoped size credit consumed by
    quality_gate.score_prospect) at load time so a config typo fails at
    startup for every consumer — daily, weekly, and rescore — instead of
    aborting a wet run mid-batch at the first scored prospect. (PR-227)
    """
    with open(CONTENT_DIR / "personas.json") as f:
        personas = json.load(f)
    _size_fields = {"search_size_credit", "search_headcount_filter"}
    for key, cfg in personas.items():
        if not isinstance(cfg, dict):
            continue
        # Near-miss key detection (adversarial-QA, operator lens): a typo'd
        # key ("search_size_credits", "search_size_cedit") would be silently
        # ignored and the persona would abstain on size — the config-level
        # re-run of the exact silent-default bug the 2026-07-06 RCA fixed.
        # Catch anything that looks like an attempt at the size fields.
        for k in cfg:
            if k in _size_fields:
                continue
            if k.startswith("search_size") or k.startswith("search_headcount"):
                close = (
                    "search_size_credit" if "size" in k
                    else "search_headcount_filter"
                )
                raise ValueError(
                    f"personas.json: {key} has unrecognized field {k!r} — "
                    f"did you mean {close!r}? (a typo'd key is silently "
                    "ignored and the persona would abstain on size)"
                )
        credit = cfg.get("search_size_credit")
        if credit is None:
            continue
        if isinstance(credit, bool) or not isinstance(credit, int) or not 0 <= credit <= 30:
            raise ValueError(
                f"personas.json: {key}.search_size_credit={credit!r} must be "
                "an integer 0-30 (the size component's range — see "
                "workflows/quality_gate.py score_prospect)."
            )
    return personas


def load_targets() -> list[dict]:
    """Load target companies from targets.json."""
    with open(CONTENT_DIR / "targets.json") as f:
        data = json.load(f)
    return data["companies"] if isinstance(data, dict) else data


def get_message(
    persona: Persona,
    language: Language,
    step: MessageStep,
    *,
    record_id: str | None = None,
) -> str:
    """Get the raw message template for a persona/language/step combination.

    PR-16: raises `MissingMessageError` when the `(persona, language,
    step)` triple has no body. The pre-PR-16 fallback to `step_msgs["es"]`
    silently shipped Spanish copy to English/Portuguese prospects — a
    §0 #9 violation closed here.

    `record_id` is an optional context field that callers pass through
    to enrich the error payload for operator triage.
    """
    messages = load_messages()
    persona_msgs = messages.get(persona.value)
    if persona_msgs is None:
        raise MissingMessageError(
            persona=persona.value,
            language=language.value,
            dm_step=step.value,
            variant="default",
            record_id=record_id,
        )
    step_msgs = persona_msgs.get(step.value)
    if step_msgs is None:
        raise MissingMessageError(
            persona=persona.value,
            language=language.value,
            dm_step=step.value,
            variant="default",
            record_id=record_id,
        )
    body = step_msgs.get(language.value)
    if not body:
        raise MissingMessageError(
            persona=persona.value,
            language=language.value,
            dm_step=step.value,
            variant="default",
            record_id=record_id,
        )
    return body


# ── Pain-signal lane (PR-280 / PR-284) ───────────────────────────────
#
# The pain-signal discovery lane's connection notes live in a dedicated
# top-level `messages.json` group keyed by SOURCE TYPE, not by persona: the
# note references the prospect's LinkedIn post (or their reaction to one),
# which is orthogonal to which persona their title classified into. The group
# is deliberately NOT a Persona enum member — `Persona.from_attio` silently
# falls back on unknown values, so a pseudo-persona would risk shipping wrong
# copy silently. Selection happens in
# `workflows/daily_check.py::_build_invite_send_data` off the entry's
# `prospect_source == "pain_signal"` attribute; a missing (source_type,
# language) body raises `MissingMessageError` so the caller can fall back to
# the persona note LOUDLY.
PAIN_SIGNAL_MESSAGE_GROUP = "pain_signal"

# Single source for the valid pain_source_type values — the schema script
# seeds its select options from this tuple, and the note-frame mapping below
# must cover exactly these.
PAIN_SIGNAL_SOURCE_TYPES = ("poster", "commenter", "liker")

# Reference-frame mapping: the engager workers yield three source types but
# the group ships TWO reference frames. A commenter did not write the post, so
# the engagement-frame note is the honest one for them — same as a liker. Only
# a verified author gets the authorship frame.
_PAIN_NOTE_TEMPLATE_BY_SOURCE = {
    "poster": "connection_note_poster",
    "commenter": "connection_note_liker",
    "liker": "connection_note_liker",
}
assert set(_PAIN_NOTE_TEMPLATE_BY_SOURCE) == set(PAIN_SIGNAL_SOURCE_TYPES), (
    "every pain_source_type needs a note-frame mapping"
)


def get_pain_signal_note(
    language: Language,
    *,
    source_type: str,
    record_id: str | None = None,
) -> str:
    """Get the pain-signal connection-note template for a source type.

    `source_type` is `"poster"` (they wrote the matched post), `"commenter"`
    (they commented on it), or `"liker"` (they reacted to it). Posters get the
    authorship reference frame; commenters and likers share the engagement
    frame — claiming a non-author wrote the post would be wrong on the wire.
    Unknown source types raise `MissingMessageError` (never guess which
    reference frame to use).
    """
    messages = load_messages()
    group = messages.get(PAIN_SIGNAL_MESSAGE_GROUP) or {}
    step_key = _PAIN_NOTE_TEMPLATE_BY_SOURCE.get(source_type)
    body = (group.get(step_key) or {}).get(language.value) if step_key else None
    if not body:
        raise MissingMessageError(
            persona=PAIN_SIGNAL_MESSAGE_GROUP,
            language=language.value,
            dm_step=step_key or f"connection_note_{source_type}",
            variant="default",
            record_id=record_id,
        )
    return body


INDUSTRY_LABELS: dict[str, dict[str, str]] = {
    "Manufacturing": {"es": "manufactura", "en": "manufacturing", "pt": "manufatura"},
    "Food & Beverage": {"es": "alimentos y bebidas", "en": "food & beverage", "pt": "alimentos e bebidas"},
    "Construction": {"es": "construcción", "en": "construction", "pt": "construção"},
    "Oil & Gas": {"es": "petróleo y gas", "en": "oil & gas", "pt": "petróleo e gás"},
    "Mining": {"es": "minería", "en": "mining", "pt": "mineração"},
    "Automotive": {"es": "automotriz", "en": "automotive", "pt": "automotiva"},
    "Textiles": {"es": "textil", "en": "textiles", "pt": "têxtil"},
    "Packaging": {"es": "empaque", "en": "packaging", "pt": "embalagens"},
    "Chemicals": {"es": "química", "en": "chemicals", "pt": "química"},
    "Pharma": {"es": "farmacéutica", "en": "pharma", "pt": "farmacêutica"},
    "Other": {"es": "manufactura", "en": "manufacturing", "pt": "manufatura"},
}

# Module-load invariant: every Language enum value must have a localized label
# for every industry, or get_industry_label() silently falls back to Spanish
# for the new language without any indication. Catching this at import time
# means a future Language addition can't ship without the labels.
_REQUIRED_LANGS = {lang.value for lang in Language}
for _industry, _labels in INDUSTRY_LABELS.items():
    _missing = _REQUIRED_LANGS - set(_labels.keys())
    assert not _missing, (
        f"INDUSTRY_LABELS[{_industry!r}] missing labels for languages "
        f"{sorted(_missing)} — add them before adding a Language enum value"
    )


def get_industry_label(industry: str | None, language: Language) -> str:
    """Get the localized label for an Attio industry_vertical value.

    Unknown or empty industries fall back to the generic manufacturing label
    for the given language — we never want to leave an industry placeholder
    unresolved in an outgoing message.
    """
    labels = INDUSTRY_LABELS.get(industry) if industry else None
    if labels is None:
        labels = INDUSTRY_LABELS["Other"]
    return labels.get(language.value, labels["es"])


def personalize(
    template: str,
    name: str,
    company: str,
    industry: str = "",
    language: Language | None = None,
) -> str:
    """Replace [Name], [Company], and industry placeholders in a message template.

    Industry placeholders are ALWAYS substituted. When no industry is supplied,
    the legacy ``[industria similar]`` / ``[similar industry]`` / ``[indústria
    similar]`` tokens fall back to a language-appropriate generic label
    ("manufactura" / "manufacturing" / "manufatura") — kept for backward
    compatibility.

    The ``[industry_clause_es]`` / ``[industry_clause_en]`` / ``[industry_clause_pt]``
    tokens implement a *structural* fallback for the operations_leaders dm2 social-proof
    sentence: when industry IS known the clause reads "en una planta de <industria>"
    (industry-specific social proof); when unknown the industry mention drops entirely
    and the clause reads "en otra planta" (ES) / "at another plant" (EN) /
    "em outra planta" (PT), preserving the real-example claim without a deflating
    generic label.
    """
    lang = language or Language.ES

    # --- structural industry clause (industry-specific vs generic-drop) ---
    # ``industry`` may be either an Attio raw key (e.g. "Automotive") or an
    # already-localized label (e.g. "automotriz") — both count as "known" and
    # are used verbatim in the clause so we never double-translate.
    if industry:
        clause_es = f"en una planta de {industry}"
        clause_en = f"at a {industry} plant"
        clause_pt = f"em uma planta de {industry}"
    else:
        clause_es = "en otra planta"
        clause_en = "at another plant"
        clause_pt = "em outra planta"

    result = template.replace("[Name]", name).replace("[Company]", company)
    result = (
        result.replace("[industry_clause_es]", clause_es)
        .replace("[industry_clause_en]", clause_en)
        .replace("[industry_clause_pt]", clause_pt)
    )

    # --- legacy flat-industry placeholders (backward compat) ---
    # These tokens still appear in templates outside operations_leaders dm2.
    # Use the industry value directly (callers have already localized it), or
    # fall back to the generic label when no industry is provided.
    legacy_label = industry if industry else get_industry_label("", lang)
    result = (
        result.replace("[industria similar]", legacy_label)
        .replace("[similar industry]", legacy_label)
        .replace("[indústria similar]", legacy_label)
    )
    return result
