"""Offline metering audit over archived feeder topologies.

This module is a *separate* layer from the demand-bill engine.  It never books
money and never touches the ledger; it reads archived synthetic meter data and
answers two questions:

A. **Where can one known bias land?**  A case gives a period ``[a,b)`` and a
   positive deviation power ``d``.  Exactly one leaf meter under-records its
   true power by ``d`` for the whole case.  A *branch check* compares a branch
   total meter against the leaf meters that truly belong to that branch during
   the check period (split at every topology epoch, because a meter can be
   re-tapped at an epoch boundary while keeping its identity).  For a candidate
   leaf ``L`` the *predicted difference* is ``d`` times the time ``L`` really
   belongs to the checked branch inside the check period.  The check is a
   *constraint* only when the total meter and every at-the-time leaf meter are
   fully known over the period; otherwise it lists the missing-evidence ranges
   and constrains nothing.  A constraint keeps exactly those candidates whose
   predicted difference equals the measured difference.

B. **In what order should sealed review files be opened?**  Each archive file
   is a leaf set with a positive integer cost and answers "yes" iff the true
   deviating leaf is in the set.  We compute an adaptive strategy that reads
   one file at a time (the next choice may depend on earlier answers) and
   minimises the worst-case total cost of identifying the leaf; ties at a step
   go to the smallest file number.

All arithmetic is exact :class:`fractions.Fraction`.  Time and power come from
the strict rational parser, so JSON floats are rejected.

Three mutually exclusive audit outcomes are reported: ``unique`` (one leaf),
``contradiction`` (no leaf is consistent with the measurements), and
``multiple`` (several candidates remain).  If some candidates answer every
available archive file identically they are reported as an indistinguishable
class and the result is explicitly *not* claimed to be locatable.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from .errors import AuditError
from .interval import Segment, Trajectory, ZERO, frac_to_json
from .numbers import parse_frac

# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _frac(node: Any, field: str) -> Fraction:
    try:
        return parse_frac(node, field)
    except AuditError:
        raise
    except Exception as exc:  # ValidationError from numbers
        raise AuditError(f"{field}: {exc}") from exc


def _fj(x: Fraction | None) -> dict | None:
    return frac_to_json(x)


def _fs(x: Fraction) -> str:
    """Human-readable exact fraction, e.g. ``25/2`` or ``60``."""
    return str(x)


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Node:
    node_id: str
    parent: str | None
    kind: str                 # "branch" | "leaf"
    total_meter: str | None   # only meaningful for a branch


@dataclass(frozen=True, slots=True)
class Epoch:
    epoch_id: str
    start: Fraction
    end: Fraction
    nodes: tuple[Node, ...]


class Topology:
    """A revisioned sequence of rooted feeder trees, one per epoch.

    Leaves are stable user meters: the leaf-id set must be identical in every
    epoch, though a leaf may sit under a different branch after a reconnect.
    """

    def __init__(self, topology_id: str, revision: str,
                 epochs: Sequence[Epoch]):
        if not topology_id:
            raise AuditError("topology_id is required")
        self.topology_id = topology_id
        self.revision = revision
        ordered = tuple(sorted(epochs, key=lambda e: (e.start, e.end)))
        if not ordered:
            raise AuditError("topology needs at least one epoch")
        prev_end: Fraction | None = None
        for ep in ordered:
            if not (ep.start < ep.end):
                raise AuditError(f"epoch {ep.epoch_id!r}: start must precede end")
            if prev_end is not None and ep.start < prev_end:
                raise AuditError(f"epochs overlap at {ep.start}")
            prev_end = ep.end
        self.epochs = ordered

        # Per-epoch derived indexes.
        self._children: dict[str, dict[str, list[str]]] = {}
        self._nodes: dict[str, dict[str, Node]] = {}
        self._branch_by_meter: dict[str, dict[str, Node]] = {}
        self._leaves_under: dict[tuple[str, str], tuple[str, ...]] = {}

        leaf_sets: list[set[str]] = []
        for ep in ordered:
            ids: dict[str, Node] = {}
            children: dict[str, list[str]] = {}
            branch_by_meter: dict[str, Node] = {}
            roots: list[str] = []
            for nd in ep.nodes:
                if nd.node_id in ids:
                    raise AuditError(
                        f"epoch {ep.epoch_id!r}: duplicate node {nd.node_id!r}")
                if nd.kind not in ("branch", "leaf"):
                    raise AuditError(
                        f"node {nd.node_id!r}: kind must be branch or leaf")
                ids[nd.node_id] = nd
            for nd in ep.nodes:
                if nd.parent is None:
                    if nd.kind != "branch":
                        raise AuditError(
                            f"root {nd.node_id!r} must be a branch")
                    roots.append(nd.node_id)
                else:
                    if nd.parent not in ids:
                        raise AuditError(
                            f"node {nd.node_id!r}: unknown parent {nd.parent!r}")
                    children.setdefault(nd.parent, []).append(nd.node_id)
                if nd.kind == "branch" and nd.total_meter is not None:
                    if nd.total_meter in branch_by_meter:
                        raise AuditError(
                            f"epoch {ep.epoch_id!r}: total meter "
                            f"{nd.total_meter!r} used by two branches")
                    if nd.total_meter in ids:
                        raise AuditError(
                            f"epoch {ep.epoch_id!r}: total meter "
                            f"{nd.total_meter!r} collides with a node id")
                    branch_by_meter[nd.total_meter] = nd
            for nd in ep.nodes:
                if nd.kind == "leaf" and children.get(nd.node_id):
                    raise AuditError(
                        f"node {nd.node_id!r}: a leaf cannot have children")
            if len(roots) != 1:
                raise AuditError(
                    f"epoch {ep.epoch_id!r}: expected exactly one root, "
                    f"found {len(roots)}")
            # Reachability from the single root proves it is a tree.
            seen: set[str] = set()
            stack = [roots[0]]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    raise AuditError(f"epoch {ep.epoch_id!r}: cycle at {cur!r}")
                seen.add(cur)
                stack.extend(children.get(cur, []))
            if len(seen) != len(ids):
                raise AuditError(
                    f"epoch {ep.epoch_id!r}: nodes not connected to the root")
            leaf_sets.append({i for i, nd in ids.items() if nd.kind == "leaf"})
            self._children[ep.epoch_id] = children
            self._nodes[ep.epoch_id] = ids
            self._branch_by_meter[ep.epoch_id] = branch_by_meter
            # Descendant leaves for every node.
            for nid in ids:
                acc: set[str] = set()
                st = [nid]
                while st:
                    cur = st.pop()
                    if ids[cur].kind == "leaf":
                        acc.add(cur)
                    st.extend(children.get(cur, []))
                self._leaves_under[(ep.epoch_id, nid)] = tuple(sorted(acc))

        base = leaf_sets[0]
        for ep, ls in zip(ordered[1:], leaf_sets[1:]):
            if ls != base:
                raise AuditError(
                    "leaf identity set must be stable across epochs; "
                    f"epoch {ep.epoch_id!r} differs")
        self.leaf_ids: tuple[str, ...] = tuple(sorted(base))

    # -- queries ---------------------------------------------------------- #
    def branch_in_epoch(self, epoch_id: str, total_meter: str) -> Node | None:
        return self._branch_by_meter[epoch_id].get(total_meter)

    def leaves_under(self, epoch_id: str, node_id: str) -> tuple[str, ...]:
        return self._leaves_under[(epoch_id, node_id)]

    def is_branch_meter_known(self, total_meter: str) -> bool:
        return any(total_meter in m for m in self._branch_by_meter.values())

    def epoch_overlaps(self, a: Fraction, b: Fraction) -> list[tuple[Fraction, Fraction, Epoch]]:
        out: list[tuple[Fraction, Fraction, Epoch]] = []
        for ep in self.epochs:
            s, e = max(a, ep.start), min(b, ep.end)
            if s < e:
                out.append((s, e, ep))
        return out


def node_from_json(obj: Mapping[str, Any]) -> Node:
    kind = str(obj.get("kind", "leaf"))
    total = obj.get("total_meter")
    return Node(
        node_id=str(obj["id"]),
        parent=None if obj.get("parent") is None else str(obj["parent"]),
        kind=kind,
        total_meter=None if total is None else str(total),
    )


def parse_topology(obj: Mapping[str, Any]) -> Topology:
    try:
        epochs = [
            Epoch(
                epoch_id=str(ep["epoch_id"]),
                start=_frac(ep["start"], "epoch start"),
                end=_frac(ep["end"], "epoch end"),
                nodes=tuple(node_from_json(n) for n in ep["nodes"]),
            )
            for ep in obj["epochs"]
        ]
        return Topology(str(obj["topology_id"]), str(obj["revision"]), epochs)
    except KeyError as exc:
        raise AuditError(f"topology is missing field {exc}") from exc


class TopologyStore:
    """Revisioned in-memory topology registry."""

    def __init__(self) -> None:
        self._revs: dict[str, Topology] = {}
        self._current: str | None = None

    def register(self, topology: Topology) -> Topology:
        if topology.revision in self._revs:
            raise AuditError(f"topology revision {topology.revision!r} exists")
        self._revs[topology.revision] = topology
        self._current = topology.revision
        return topology

    def register_json(self, obj: Mapping[str, Any]) -> Topology:
        return self.register(parse_topology(obj))

    def current(self) -> Topology:
        if self._current is None:
            raise AuditError("no topology registered")
        return self._revs[self._current]

    def revise_json(self, obj: Mapping[str, Any], *,
                    expected_revision: str | None = None) -> Topology:
        if expected_revision is not None and self._current != expected_revision:
            raise AuditError(
                f"topology is at {self._current!r}, expected {expected_revision!r}")
        return self.register_json(obj)


# --------------------------------------------------------------------------- #
# Meter readings
# --------------------------------------------------------------------------- #
def parse_readings(obj: Mapping[str, Any]) -> tuple[str, dict[str, Trajectory]]:
    """Return ``(revision, {meter_id: Trajectory})`` from a readings document."""
    try:
        revision = str(obj["revision"])
        meters_obj = obj["meters"]
    except KeyError as exc:
        raise AuditError(f"readings is missing field {exc}") from exc
    meters: dict[str, Trajectory] = {}
    for meter_id, segs in meters_obj.items():
        parsed = []
        for i, s in enumerate(segs):
            parsed.append(Segment(
                segment_id=f"{meter_id}#{i}",
                start=_frac(s["start"], f"{meter_id} start"),
                end=_frac(s["end"], f"{meter_id} end"),
                power=_frac(s["power"], f"{meter_id} power"),
            ))
        meters[str(meter_id)] = Trajectory(parsed)
    return revision, meters


class MeterReadingStore:
    """Revisioned measured-power trajectories, one per meter."""

    def __init__(self) -> None:
        self._revs: dict[str, dict[str, Trajectory]] = {}
        self._current: str | None = None

    def register_json(self, obj: Mapping[str, Any]) -> str:
        revision, meters = parse_readings(obj)
        if revision in self._revs:
            raise AuditError(f"readings revision {revision!r} exists")
        self._revs[revision] = meters
        self._current = revision
        return revision

    def current(self) -> tuple[str, dict[str, Trajectory]]:
        if self._current is None:
            raise AuditError("no readings registered")
        return self._current, self._revs[self._current]

    def revise_json(self, obj: Mapping[str, Any], *,
                    expected_revision: str | None = None) -> str:
        """Merge ``obj['meters']`` onto the current revision under a new id.

        A meter mapped to ``null`` is removed; any other value replaces that
        meter's trajectory.  Unmentioned meters are carried over unchanged.
        This is the meter-revision path the audit must actually adopt.
        """
        if expected_revision is not None and self._current != expected_revision:
            raise AuditError(
                f"readings is at {self._current!r}, expected {expected_revision!r}")
        revision = str(obj["revision"])
        if revision in self._revs:
            raise AuditError(f"readings revision {revision!r} exists")
        if self._current is None:
            raise AuditError("no readings registered")
        merged = dict(self._revs[self._current])
        overrides = obj.get("meters", {})
        for meter_id, segs in overrides.items():
            if segs is None:
                merged.pop(str(meter_id), None)
                continue
            parsed = [
                Segment(
                    segment_id=f"{meter_id}#{i}",
                    start=_frac(s["start"], f"{meter_id} start"),
                    end=_frac(s["end"], f"{meter_id} end"),
                    power=_frac(s["power"], f"{meter_id} power"),
                )
                for i, s in enumerate(segs)
            ]
            merged[str(meter_id)] = Trajectory(parsed)
        self._revs[revision] = merged
        self._current = revision
        return revision


# --------------------------------------------------------------------------- #
# Case, checks, results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AuditCase:
    case_id: str
    period_start: Fraction
    period_end: Fraction
    deviation_power: Fraction

    @property
    def period(self) -> tuple[Fraction, Fraction]:
        return self.period_start, self.period_end


def parse_case(obj: Mapping[str, Any]) -> AuditCase:
    try:
        a, b = obj["period"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditError("case period must be a [start, end] pair") from exc
    case = AuditCase(
        case_id=str(obj.get("case_id", "case")),
        period_start=_frac(a, "case period start"),
        period_end=_frac(b, "case period end"),
        deviation_power=_frac(obj["deviation_power"], "deviation_power"),
    )
    if not (case.period_start < case.period_end):
        raise AuditError("case period must have positive length")
    if case.deviation_power <= 0:
        raise AuditError("deviation_power must be positive")
    return case


@dataclass(frozen=True, slots=True)
class Check:
    check_id: str
    branch_meter: str
    period_start: Fraction
    period_end: Fraction

    @property
    def period(self) -> tuple[Fraction, Fraction]:
        return self.period_start, self.period_end


def parse_check(obj: Mapping[str, Any]) -> Check:
    try:
        a, b = obj["period"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditError("check period must be a [start, end] pair") from exc
    chk = Check(
        check_id=str(obj["check_id"]),
        branch_meter=str(obj["branch"]),
        period_start=_frac(a, "check period start"),
        period_end=_frac(b, "check period end"),
    )
    if not (chk.period_start < chk.period_end):
        raise AuditError(f"check {chk.check_id!r}: period must have positive length")
    return chk


@dataclass(frozen=True, slots=True)
class MissingRange:
    meter: str | None
    reason: str
    start: Fraction
    end: Fraction

    def to_json(self) -> dict:
        return {
            "meter": self.meter,
            "reason": self.reason,
            "start": _fj(self.start),
            "end": _fj(self.end),
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: Check
    status: str                        # "constraint" | "missing_evidence"
    measured_difference: Fraction | None
    predicted_difference: dict[str, Fraction]
    matches: tuple[str, ...]
    missing: tuple[MissingRange, ...]
    explanation: str

    def to_json(self) -> dict:
        return {
            "check_id": self.check.check_id,
            "branch": self.check.branch_meter,
            "period": [_fj(self.check.period_start), _fj(self.check.period_end)],
            "status": self.status,
            "measured_difference": _fj(self.measured_difference),
            "predicted_difference": {
                leaf: _fj(d) for leaf, d in sorted(self.predicted_difference.items())
            },
            "matches": list(self.matches),
            "missing": [m.to_json() for m in self.missing],
            "explanation": self.explanation,
        }


def _subtract(base: tuple[Fraction, Fraction],
              cuts: Iterable[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    a, b = base
    clamped = sorted(
        (max(a, c), min(b, d)) for c, d in cuts if min(b, d) > max(a, c)
    )
    out: list[tuple[Fraction, Fraction]] = []
    cur = a
    for c, d in clamped:
        if c > cur:
            out.append((cur, c))
        cur = max(cur, d)
    if cur < b:
        out.append((cur, b))
    return out


class MeterAudit:
    """Runs branch checks and narrows the single-biased-leaf candidate set."""

    def __init__(self, topology: Topology, readings: Mapping[str, Trajectory],
                 case: AuditCase, *, topology_revision: str | None = None,
                 readings_revision: str | None = None):
        self.topology = topology
        self.readings = dict(readings)
        self.case = case
        self.topology_revision = topology_revision or topology.revision
        self.readings_revision = readings_revision

    # -- a single branch check ------------------------------------------- #
    def run_check(self, check: Check) -> CheckResult:
        a, b = check.period
        d = self.case.deviation_power
        leaves = self.topology.leaf_ids
        duration: dict[str, Fraction] = {leaf: ZERO for leaf in leaves}
        missing: list[MissingRange] = []
        measurable = True
        measured = ZERO

        spans = self.topology.epoch_overlaps(a, b)
        for ms, me in _subtract((a, b), [(s, e) for s, e, _ in spans]):
            missing.append(MissingRange(None, "no_topology", ms, me))
            measurable = False

        for s, e, ep in spans:
            node = self.topology.branch_in_epoch(ep.epoch_id, check.branch_meter)
            if node is None:
                missing.append(MissingRange(check.branch_meter, "branch_absent", s, e))
                measurable = False
                continue
            under = self.topology.leaves_under(ep.epoch_id, node.node_id)
            for leaf in under:
                duration[leaf] += (e - s)

            span_ok = True
            total_traj = self.readings.get(check.branch_meter)
            if total_traj is None:
                missing.append(MissingRange(check.branch_meter, "no_reading", s, e))
                span_ok = False
                total_energy = ZERO
            else:
                _, _, gaps = total_traj.coverage(s, e)
                for gs, ge in gaps:
                    missing.append(MissingRange(check.branch_meter, "gap", gs, ge))
                span_ok = span_ok and not gaps
                total_energy = total_traj.integral(s, e)

            leaf_energy = ZERO
            for leaf in under:
                traj = self.readings.get(leaf)
                if traj is None:
                    missing.append(MissingRange(leaf, "no_reading", s, e))
                    span_ok = False
                    continue
                _, _, gaps = traj.coverage(s, e)
                if gaps:
                    for gs, ge in gaps:
                        missing.append(MissingRange(leaf, "gap", gs, ge))
                    span_ok = False
                else:
                    leaf_energy += traj.integral(s, e)

            if span_ok:
                measured += total_energy - leaf_energy
            else:
                measurable = False

        predicted = {leaf: d * dur for leaf, dur in duration.items()}

        if measurable:
            matches = tuple(leaf for leaf in leaves if predicted[leaf] == measured)
            if matches:
                explanation = (
                    f"constraint: measured difference {_fs(measured)} matches "
                    f"predicted difference of {list(matches)}"
                )
            else:
                attainable = sorted({predicted[leaf] for leaf in leaves})
                shown = ", ".join(_fs(x) for x in attainable)
                explanation = (
                    f"constraint: measured difference {_fs(measured)} matches "
                    f"no leaf; predicted differences are {{{shown}}}"
                )
            return CheckResult(
                check=check, status="constraint",
                measured_difference=measured,
                predicted_difference=predicted,
                matches=matches, missing=(), explanation=explanation,
            )

        missing.sort(key=lambda m: (m.start, m.end, m.meter or ""))
        reason = (
            f"missing evidence over {len(missing)} range(s); "
            "this check is not a constraint"
        )
        return CheckResult(
            check=check, status="missing_evidence",
            measured_difference=None,
            predicted_difference=predicted,
            matches=(), missing=tuple(missing), explanation=reason,
        )

    # -- narrowing ------------------------------------------------------- #
    def audit(self, checks: Sequence[Check]) -> dict:
        results = [self.run_check(c) for c in checks]
        candidates = set(self.topology.leaf_ids)
        trace: list[dict] = []
        contradiction_check: str | None = None

        for cr in results:
            if cr.status != "constraint":
                trace.append({
                    "check_id": cr.check.check_id,
                    "action": "ignored_missing_evidence",
                    "remaining": sorted(candidates),
                })
                continue
            keep = {leaf for leaf in candidates if cr.predicted_difference[leaf]
                    == cr.measured_difference}
            removed = sorted(candidates - keep)
            before = sorted(candidates)
            candidates = keep
            trace.append({
                "check_id": cr.check.check_id,
                "action": "constraint_applied",
                "remaining": sorted(candidates),
                "removed": removed,
            })
            if not candidates:
                contradiction_check = cr.check.check_id
                break

        status, explanation = self._outcome(results, candidates, contradiction_check)
        return {
            "case": {
                "case_id": self.case.case_id,
                "period": [_fj(self.case.period_start), _fj(self.case.period_end)],
                "deviation_power": _fj(self.case.deviation_power),
            },
            "topology_id": self.topology.topology_id,
            "topology_revision": self.topology_revision,
            "readings_revision": self.readings_revision,
            "leaf_universe": list(self.topology.leaf_ids),
            "checks": [cr.to_json() for cr in results],
            "trace": trace,
            "candidates": sorted(candidates),
            "status": status,
            "explanation": explanation,
        }

    def _outcome(self, results: Sequence[CheckResult],
                 candidates: set[str],
                 contradiction_check: str | None) -> tuple[str, str]:
        if not candidates:
            detail = ""
            for cr in results:
                if cr.check.check_id == contradiction_check:
                    detail = cr.explanation
                    break
            return (
                "contradiction",
                "measurements cannot be explained by exactly one leaf "
                f"under-recording by d={_fs(self.case.deviation_power)}. "
                f"Emptied by check {contradiction_check!r}: {detail}",
            )
        if len(candidates) == 1:
            return "unique", f"exactly one leaf remains: {next(iter(candidates))!r}"
        constraint_count = sum(1 for cr in results if cr.status == "constraint")
        if constraint_count == 0:
            return (
                "multiple",
                f"no complete check was available; {len(candidates)} candidates "
                "cannot be narrowed",
            )
        return (
            "multiple",
            f"{len(candidates)} candidates remain after {constraint_count} "
            f"constraint check(s): {sorted(candidates)}",
        )


# --------------------------------------------------------------------------- #
# Archive strategy (minimise worst-case total read cost)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ArchiveFile:
    file_no: int
    cost: Fraction
    leaves: frozenset[str]


def parse_archives(obj: Mapping[str, Any]) -> list[ArchiveFile]:
    try:
        files = obj["files"]
    except KeyError as exc:
        raise AuditError("archives is missing 'files'") from exc
    out: list[ArchiveFile] = []
    seen: set[int] = set()
    for f in files:
        no = int(f["file_no"])
        if no in seen:
            raise AuditError(f"duplicate archive file_no {no}")
        seen.add(no)
        cost = _frac(f["cost"], f"file {no} cost")
        if cost <= 0:
            raise AuditError(f"file {no}: cost must be positive")
        if cost.denominator != 1:
            raise AuditError(f"file {no}: cost must be an integer")
        out.append(ArchiveFile(no, cost, frozenset(str(x) for x in f["leaves"])))
    return out


def _signature(leaf: str, files: Sequence[ArchiveFile]) -> tuple[int, ...]:
    return tuple(f.file_no for f in files if leaf in f.leaves)


def _classes(candidates: Iterable[str],
             files: Sequence[ArchiveFile]) -> list[list[str]]:
    groups: dict[tuple[int, ...], list[str]] = {}
    for leaf in sorted(candidates):
        groups.setdefault(_signature(leaf, files), []).append(leaf)
    return list(groups.values())


def _solve(candidates: frozenset[str], files: Sequence[ArchiveFile],
           memo: dict[frozenset[str], tuple[Fraction, dict]]) -> tuple[Fraction, dict]:
    if candidates in memo:
        return memo[candidates]
    if len(candidates) <= 1:
        node = {"action": "identified", "leaves": sorted(candidates)}
        memo[candidates] = (ZERO, node)
        return memo[candidates]

    if len(_classes(candidates, files)) == 1:
        node = {"action": "indistinguishable", "leaves": sorted(candidates)}
        memo[candidates] = (ZERO, node)
        return memo[candidates]

    best: tuple[tuple[Fraction, int], ArchiveFile, Fraction, dict,
                 Fraction, dict] | None = None
    for f in sorted(files, key=lambda x: x.file_no):
        yes = candidates & f.leaves
        no = candidates - f.leaves
        if not yes or not no:
            continue  # this file does not split the current set
        cy, ny = _solve(frozenset(yes), files, memo)
        cn, nn = _solve(frozenset(no), files, memo)
        worst = f.cost + max(cy, cn)
        key = (worst, f.file_no)
        if best is None or key < best[0]:
            best = (key, f, cy, ny, cn, nn)

    if best is None:  # pragma: no cover - guarded by the class check above
        node = {"action": "indistinguishable", "leaves": sorted(candidates)}
        memo[candidates] = (ZERO, node)
        return memo[candidates]

    (worst, _), f, cy, ny, cn, nn = best
    node = {
        "action": "read",
        "file_no": f.file_no,
        "cost": _fj(f.cost),
        "yes": {**ny, "worst_cost": _fj(cy)},
        "no": {**nn, "worst_cost": _fj(cn)},
        "worst_cost": _fj(worst),
    }
    memo[candidates] = (worst, node)
    return memo[candidates]


def optimal_strategy(candidates: Iterable[str],
                     archives: Sequence[ArchiveFile],
                     open_files: Iterable[int] | None = None) -> dict:
    """Compute an adaptive min-worst-cost identification strategy.

    ``open_files`` restricts which archive files may be read (file numbers).
    Returns a report with the worst-case cost, the first file, the full
    decision tree, and any indistinguishable leaf classes.
    """
    cand = frozenset(candidates)
    if len(cand) > 10:
        raise AuditError(f"at most 10 candidate leaves are supported, got {len(cand)}")
    open_set = None if open_files is None else {int(x) for x in open_files}
    usable = [f for f in archives if open_set is None or f.file_no in open_set]
    usable.sort(key=lambda f: f.file_no)
    if len(usable) > 12:
        raise AuditError(f"at most 12 archive files are supported, got {len(usable)}")
    open_nos = [f.file_no for f in usable]

    classes = _classes(cand, usable)
    ambiguous = [sorted(c) for c in classes if len(c) > 1]

    if not cand:
        return {
            "candidates": [],
            "open_files": open_nos,
            "status": "contradiction",
            "worst_cost": _fj(ZERO),
            "first_file": None,
            "identifiable": False,
            "indistinguishable": [],
            "tree": {"action": "contradiction"},
            "explanation": "no candidate to identify",
        }

    memo: dict[frozenset[str], tuple[Fraction, dict]] = {}
    worst, tree = _solve(cand, usable, memo)
    first_file = tree.get("file_no") if tree.get("action") == "read" else None
    identifiable = not ambiguous
    if len(cand) == 1:
        status = "unique"
        explanation = f"already unique: {sorted(cand)[0]!r}; no reads required"
    elif identifiable:
        status = "identifiable"
        explanation = (
            f"all {len(cand)} candidates are separable; worst-case cost "
            f"{_fs(worst)} starting with file {first_file}"
        )
    else:
        status = "indistinguishable"
        explanation = (
            "some candidates answer every open file identically and cannot be "
            f"separated: {ambiguous}; worst-case cost {_fs(worst)} to resolve "
            "the remaining classes"
        )
    return {
        "candidates": sorted(cand),
        "open_files": open_nos,
        "status": status,
        "worst_cost": _fj(worst),
        "first_file": first_file,
        "identifiable": identifiable,
        "indistinguishable": ambiguous,
        "tree": tree,
        "explanation": explanation,
    }


# --------------------------------------------------------------------------- #
# Scenario runner
# --------------------------------------------------------------------------- #
def run_scenario(obj: Mapping[str, Any], *,
                 apply_revision: bool = True,
                 open_variants: Sequence[Sequence[int]] | None = None) -> dict:
    """Run a self-contained scenario document and return a JSON-ready report.

    The document carries a case, a topology, initial readings, optional
    ``readings_revision_to`` overrides, checks, archives, and named strategy
    queries.  When ``readings_revision_to`` is present the audit is run again
    on the revised readings so the adopted revision is visible.
    """
    case = parse_case(obj["case"])
    topology = parse_topology(obj["topology"])
    store = MeterReadingStore()
    rev1 = store.register_json(obj["readings"])
    checks = [parse_check(c) for c in obj.get("checks", [])]
    archives = parse_archives(obj["archives"]) if "archives" in obj else []

    def _audit(rev: str, meters: Mapping[str, Trajectory],
               selected: Sequence[Check]) -> dict:
        return MeterAudit(
            topology, meters, case,
            topology_revision=topology.revision, readings_revision=rev,
        ).audit(selected)

    by_id = {c.check_id: c for c in checks}
    rev_cur, meters_cur = store.current()
    initial = _audit(rev_cur, meters_cur, checks)

    revised: dict | None = None
    if apply_revision and "readings_revision_to" in obj:
        rev2 = store.revise_json(obj["readings_revision_to"],
                                 expected_revision=rev_cur)
        rev_cur, meters_cur = store.current()
        revised = _audit(rev_cur, meters_cur, checks)

    final_audit = revised if revised is not None else initial

    strategies: list[dict] = []
    for spec in obj.get("strategies", []):
        # A strategy may name a subset of checks (checks obtained so far);
        # default is every check.  ``candidates`` may instead be "initial"
        # or "final".
        check_ids = spec.get("checks")
        if check_ids is not None:
            unknown = [c for c in check_ids if c not in by_id]
            if unknown:
                raise AuditError(f"strategy names unknown checks {unknown}")
            audit = _audit(rev_cur, meters_cur, [by_id[c] for c in check_ids])
            cands = audit["candidates"]
        else:
            source = spec.get("candidates", "final")
            cands = (initial if source == "initial" else final_audit)["candidates"]
        strategies.append({
            "name": spec.get("name", "strategy"),
            **optimal_strategy(cands, archives,
                               open_files=spec.get("open_files")),
        })

    variants = None
    if open_variants is not None:
        variants = [
            {"open_files": list(v),
             **optimal_strategy(final_audit["candidates"], archives,
                                open_files=v)}
            for v in open_variants
        ]

    return {
        "scenario_id": str(obj.get("scenario_id", "scenario")),
        "initial": initial,
        "revised": revised,
        "final_candidates": final_audit["candidates"],
        "final_status": final_audit["status"],
        "strategies": strategies,
        "open_file_variants": variants,
    }
