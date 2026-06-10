"""CRM seam: the vendor-neutral ``CRMProvider`` contract + normalized types.

Public surface re-exported here so callers import from ``clients.crm`` rather
than reaching into ``clients.crm.base``. Concrete adapters (Attio reference
adapter, factory) arrive in later Phase 1 subtasks.
"""

from clients.crm.attio_provider import AttioProvider
from clients.crm.base import (
    CRMProvider,
    Entry,
    Record,
    RecordInfo,
    Stage,
)
from clients.crm.exceptions import CRMError, UniquenessConflictError
from clients.crm.fake_provider import FakeProvider
from clients.crm.mapping import (
    CRMMapping,
    default_attio_mapping,
    load_crm_mapping,
)

__all__ = [
    "AttioProvider",
    "CRMError",
    "CRMMapping",
    "CRMProvider",
    "Entry",
    "FakeProvider",
    "Record",
    "RecordInfo",
    "Stage",
    "UniquenessConflictError",
    "default_attio_mapping",
    "load_crm_mapping",
]
