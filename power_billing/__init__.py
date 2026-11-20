"""Public facade tying power trajectory, demand engine, and ledger together.

Typical use::

    store = TrajectoryStore()
    store.register_json(power_json)
    contract = Contract.from_json(contract_json)

    ledger = Ledger("billing.db")
    ledger.register_contract(contract)
    ledger.set_current_power(trace_id, revision)

    target = evaluate(contract, store.trajectory(trace_id), trace_id, revision)
    receipt = ledger.post_target("B1", target)

A power correction is two independent steps, on purpose:

1. ``store.revise_json(...)`` builds the new (validated) trajectory;
2. ``ledger.set_current_power(...)`` marks it current -- **booking nothing**.

Only an explicit ``post_target`` against the still-current versions moves money.
"""

from __future__ import annotations

from fractions import Fraction

from .demand import (
    COVERAGE_COMPLETE,
    COVERAGE_INSUFFICIENT,
    COVERAGE_PARTIAL,
    SCHEMA,
    Contract,
    TargetCharge,
    evaluate,
)
from .audit import (
    ArchiveFile,
    AuditCase,
    Check,
    CheckResult,
    Epoch,
    MeterAudit,
    MeterReadingStore,
    Node,
    Topology,
    TopologyStore,
    optimal_strategy,
    parse_archives,
    parse_case,
    parse_check,
    parse_topology,
    run_scenario,
)
from .errors import (
    AuditError,
    ConflictError,
    ContractError,
    InsufficientEvidence,
    NotFoundError,
    PowerBillingError,
    RevisionError,
    SegmentationError,
    ValidationError,
)
from .interval import Segment, Trajectory, period_charge
from .ledger import Ledger, bill_document
from .power import PowerRevision, TrajectoryStore

__all__ = [
    "Segment",
    "Trajectory",
    "Contract",
    "TargetCharge",
    "PowerRevision",
    "TrajectoryStore",
    "Ledger",
    "evaluate",
    "period_charge",
    "bill_document",
    "SCHEMA",
    "COVERAGE_COMPLETE",
    "COVERAGE_PARTIAL",
    "COVERAGE_INSUFFICIENT",
    "Fraction",
    "PowerBillingError",
    "ValidationError",
    "SegmentationError",
    "ContractError",
    "RevisionError",
    "InsufficientEvidence",
    "ConflictError",
    "NotFoundError",
    "AuditError",
    # offline metering audit
    "ArchiveFile",
    "AuditCase",
    "Check",
    "CheckResult",
    "Epoch",
    "MeterAudit",
    "MeterReadingStore",
    "Node",
    "Topology",
    "TopologyStore",
    "optimal_strategy",
    "parse_archives",
    "parse_case",
    "parse_check",
    "parse_topology",
    "run_scenario",
]
