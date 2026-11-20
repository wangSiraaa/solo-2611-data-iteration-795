"""Query entry point for the offline metering audit.

Usage::

    python3 -m power_billing.audit_cli materials/900_audit/scenario.json
    python3 -m power_billing.audit_cli materials/900_audit/scenario.json --json
    python3 -m power_billing.audit_cli materials/900_audit/fractional.json --strategy after_revision --open-files 1,2,3

The scenario document is self-contained (case, topology, readings, checks,
archives, optional revision and named strategies), so a run is exactly
reproducible from the file plus these flags.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import optimal_strategy, parse_archives, run_scenario


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _fmt(x) -> str:
    if x is None:
        return "-"
    return f"{x['n']}/{x['d']}"


def _print_strategy(strat: dict) -> None:
    print(
        f"  strategy {strat['name']!r}: status={strat['status']} "
        f"worst_cost={_fmt(strat['worst_cost'])} "
        f"first_file={strat['first_file']} "
        f"candidates={strat['candidates']} "
        f"open_files={strat['open_files']}"
    )
    if strat["indistinguishable"]:
        print(f"    indistinguishable classes: {strat['indistinguishable']}")
    tree = strat.get("tree", {})
    if tree.get("action") == "read":
        print(
            f"    read file {tree['file_no']} (cost {_fmt(tree['cost'])}): "
            f"yes -> worst {_fmt(tree['yes']['worst_cost'])}; "
            f"no -> worst {_fmt(tree['no']['worst_cost'])}"
        )


def _print_audit(label: str, report: dict) -> None:
    print(f"[{label}] status={report['status']} "
          f"candidates={report['candidates']} "
          f"topology={report['topology_revision']} "
          f"readings={report['readings_revision']}")
    for chk in report["checks"]:
        if chk["status"] == "constraint":
            print(
                f"  check {chk['check_id']} ({chk['branch']}): "
                f"measured={_fmt(chk['measured_difference'])} "
                f"matches={chk['matches']}"
            )
        else:
            ranges = ", ".join(
                f"{(m['meter'] or 'topology')}[{_fmt(m['start'])},{_fmt(m['end'])}):{m['reason']}"
                for m in chk["missing"]
            )
            print(f"  check {chk['check_id']} ({chk['branch']}): "
                  f"not a constraint; missing: {ranges}")
        preds = ", ".join(f"{k}={_fmt(v)}"
                          for k, v in chk["predicted_difference"].items())
        print(f"    predicted: {preds}")
    print(f"  -> {report['explanation']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", type=Path)
    ap.add_argument("--json", action="store_true", help="print full report as JSON")
    ap.add_argument("--strategy", default=None,
                    help="run an ad-hoc strategy by name-less query")
    ap.add_argument("--candidates", default=None,
                    help="comma-separated candidate leaves for --strategy")
    ap.add_argument("--open-files", default=None,
                    help="comma-separated archive file numbers to allow")
    args = ap.parse_args(argv)

    doc = _load(args.scenario)
    open_variants = None
    report = run_scenario(doc, open_variants=None)

    # Optional ad-hoc strategy query over the scenario's archives.
    if args.strategy is not None:
        if "archives" not in doc:
            print("scenario has no archives", file=sys.stderr)
            return 2
        cands = (args.candidates.split(",") if args.candidates
                 else report["final_candidates"])
        open_files = ([int(x) for x in args.open_files.split(",")]
                      if args.open_files else None)
        ad_hoc = {
            "name": args.strategy,
            **optimal_strategy(cands, parse_archives(doc["archives"]),
                               open_files=open_files),
        }
        report["strategies"].append(ad_hoc)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"scenario {report['scenario_id']}")
    _print_audit("initial", report["initial"])
    if report["revised"] is not None:
        _print_audit("revised", report["revised"])
    for strat in report["strategies"]:
        _print_strategy(strat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
