"""Fresh-process measurements. Memory tracing is separate from timing samples."""

from __future__ import annotations

import argparse
import copy
import json
import platform
import resource
import statistics
import subprocess
import sys
import tracemalloc
from pathlib import Path
from time import perf_counter

from .evidence import code_identity, counts, digest, save
from .input import parse_cases
from .policy import prepare


def scale_document(fact_count: int) -> dict:
    """Disjoint length-two chains in ONE work/context, with linear output size."""
    if fact_count < 2 or fact_count % 2:
        raise ValueError("fact_count must be positive and even")
    case = {
        "caseId": f"scale-{fact_count}",
        "title": "single-context bounded chains",
        "purpose": "measure facts and all proof outputs, without cross-case partitioning",
        "snapshotRevision": 1,
        "snapshotThroughEpisode": 1,
        "query": {
            "workId": "scale-work",
            "cutoffEpisode": 1,
            "temporalContextId": "present",
            "inputMode": "EPISODE_SNAPSHOT",
        },
        "entities": [],
        "sources": [],
        "assertions": [],
        "suppressions": [],
    }
    for chain in range(fact_count // 2):
        people = [f"person-{chain}-{position}" for position in range(3)]
        case["entities"].extend(
            {
                "entityId": person,
                "name": person,
                "workId": "scale-work",
                "identityStatus": "RESOLVED",
            }
            for person in people
        )
        for step in range(2):
            key = f"{chain}-{step}"
            case["sources"].append(
                {
                    "sourceId": f"s-{key}",
                    "workId": "scale-work",
                    "episodeNo": 1,
                    "supportStatus": "ACTIVE",
                    "quoteAvailability": "AVAILABLE",
                    "text": "synthetic parent assertion",
                    "quote": "parent assertion",
                }
            )
            case["assertions"].append(
                {
                    "assertionId": f"a-{key}",
                    "workId": "scale-work",
                    "subjectId": people[step],
                    "objectId": people[step + 1],
                    "kind": "PARENT_OF",
                    "subtype": "BIOLOGICAL",
                    "generationGap": None,
                    "assertionState": "ASSERTED",
                    "factValidity": "VALID",
                    "nativeTime": {"domain": "FAMILY", "scope": "CURRENT"},
                    "temporalContextId": "present",
                    "sourceIds": [f"s-{key}"],
                    "validationIssues": [],
                }
            )
    return {"schemaVersion": 2, "synthetic": True, "cases": [case]}


def withdraw_first_support(cases: tuple[dict, ...]) -> dict:
    changed = copy.deepcopy(list(cases))
    for case in changed:
        case["snapshotRevision"] += 1
        if case["sources"]:
            case["sources"][0]["supportStatus"] = "WITHDRAWN"
    return {"schemaVersion": 2, "synthetic": True, "cases": changed}


def _sample(document: dict, engine, extended: bool) -> tuple[dict, tuple[dict, ...]]:
    started = perf_counter()
    cases = parse_cases(document)
    parsed = perf_counter()
    prepared = [prepare(case) for case in cases]
    eligible = perf_counter()
    results, phases = [], []
    for case in prepared:
        result, times = engine.run_measured(case, extended=extended)
        results.append(result)
        phases.append(times)
    finished = perf_counter()
    totals = {
        field: sum(times[field] for times in phases)
        for field in ("constructionMs", "inferenceMs", "proofMs", "projectionMs")
    }
    totals.update(
        parseMs=(parsed - started) * 1000,
        eligibilityMs=(eligible - parsed) * 1000,
        totalMs=(finished - started) * 1000,
    )
    return {
        "times": totals,
        "outputHash": digest(results),
        "counts": {key: sum(counts(row)[key] for row in results) for key in counts(results[0])},
        "caseMetrics": phases,
    }, cases


def worker(input_path: Path, name: str, *, extended=False, memory=False) -> dict:
    imported = perf_counter()
    if name == "B":
        from . import python_engine as engine
    else:
        from . import owl_engine as engine
    import_ms = (perf_counter() - imported) * 1000
    document = json.loads(input_path.read_text(encoding="utf-8"))
    if memory:
        tracemalloc.start()
    initial, cases = _sample(document, engine, extended)
    changed = withdraw_first_support(cases)
    updated, _ = _sample(changed, engine, extended)
    peak_python = tracemalloc.get_traced_memory()[1] if memory else None
    if memory:
        tracemalloc.stop()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "engine": name,
        "memoryRun": memory,
        "engineImportMs": import_ms,
        "initial": initial,
        "fullRecompute": updated,
        "inputHash": digest(document),
        "updatedInputHash": digest(changed),
        "peakPythonBytes": peak_python,
        "processPeakRssBytes": rss if sys.platform == "darwin" else rss * 1024,
    }


def _invoke(path, name, extended, memory, timeout):
    command = [
        sys.executable,
        "-m",
        __package__ + ".benchmark",
        "--worker",
        "--input",
        str(path),
        "--engine",
        name,
    ]
    if extended:
        command.append("--extended")
    if memory:
        command.append("--memory")
    started = perf_counter()
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"benchmark {name} failed: {result.stderr[-4000:]}")
    sample = json.loads(result.stdout)
    sample["freshProcessWallMs"] = (perf_counter() - started) * 1000
    return sample


def benchmark(inputs: list[Path], output: Path, *, repeats=4, extended=False, timeout=180) -> dict:
    if repeats < 2:
        raise ValueError("at least two fresh timing processes per model are required")
    if output.exists():
        raise FileExistsError("benchmark output directory must be new")
    output.mkdir(parents=True)
    curated = []
    for path in inputs:
        curated.extend(parse_cases(json.loads(path.read_text(encoding="utf-8"))))
    documents = {
        "curated": {"schemaVersion": 2, "synthetic": True, "cases": curated},
        "facts-100": scale_document(100),
        "facts-1000": scale_document(1000),
    }
    report = {
        "formatVersion": 1,
        "identity": code_identity(),
        "profile": "grandchild-v1" if extended else "base-v1",
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "method": {
            "repeats": repeats,
            "timeoutSecondsPerProcess": timeout,
            "order": "B,C then C,B, alternating; sequential fresh processes",
            "memory": "separate traced run; timing samples have tracing disabled",
            "updates": "withdraw first source per case; fresh full recomputation",
            "total": "parse+eligibility+model construction+inference+proof+projection; excludes import/JSON IO",
        },
        "groups": {},
    }
    for group, document in documents.items():
        path = output / f"input-{group}.json"
        save(path, document)
        samples = []
        for repeat in range(repeats):
            for name in ("B", "C") if repeat % 2 == 0 else ("C", "B"):
                sample = _invoke(path, name, extended, False, timeout)
                sample["repeat"] = repeat
                samples.append(sample)
                save(output / f"{group}-{name}-{repeat}.json", sample)
                print(f"{group} {name} sample {repeat + 1}/{repeats}", flush=True)
        for name in ("B", "C"):
            sample = _invoke(path, name, extended, True, timeout)
            samples.append(sample)
            save(output / f"{group}-{name}-memory.json", sample)
        # The measured mutation is checked, too; comparing only initial output
        # could hide a stale-cache defect in the update path.
        for phase in ("initial", "fullRecompute"):
            if len({sample[phase]["outputHash"] for sample in samples}) != 1:
                raise AssertionError(f"{group} {phase}: unequal/repeat-dependent output")
        if group.startswith("facts-"):
            n = int(group.split("-")[1])
            for sample in samples:
                for phase, parents, grandparents in (
                    ("initial", n, n // 2),
                    ("fullRecompute", n - 1, n // 2 - 1),
                ):
                    derived = parents + grandparents * (2 if extended else 1)
                    expected = {
                        "direct": parents,
                        "derived": derived,
                        "proofs": derived,
                        "newRelations": grandparents,
                        "withheld": int(phase == "fullRecompute"),
                        "excludedDerived": 0,
                    }
                    if sample[phase]["counts"] != expected:
                        raise AssertionError("synthetic chain analytical counts differ")
        summary = {}
        for name in ("B", "C"):
            timings = [s for s in samples if s["engine"] == name and not s["memoryRun"]]
            memory = next(s for s in samples if s["engine"] == name and s["memoryRun"])
            summary[name] = {
                phase: {
                    field: statistics.median(s[phase]["times"][field] for s in timings)
                    for field in timings[0][phase]["times"]
                }
                for phase in ("initial", "fullRecompute")
            }
            summary[name].update(
                peakPythonBytes=memory["peakPythonBytes"],
                processPeakRssBytes=statistics.median(s["processPeakRssBytes"] for s in timings),
                initialCounts=timings[0]["initial"]["counts"],
                recomputeCounts=timings[0]["fullRecompute"]["counts"],
            )
        report["groups"][group] = {
            "inputHash": digest(document),
            "samples": samples,
            "median": summary,
            "equalOutputs": True,
        }
        save(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--engine", choices=("B", "C"))
    parser.add_argument("--memory", action="store_true")
    args = parser.parse_args()
    if args.worker:
        if not args.engine or len(args.input) != 1:
            parser.error("worker requires one input and --engine")
        print(
            json.dumps(
                worker(args.input[0], args.engine, extended=args.extended, memory=args.memory)
            )
        )
    else:
        if not args.output:
            parser.error("--output is required")
        benchmark(
            args.input,
            args.output,
            repeats=args.repeats,
            extended=args.extended,
            timeout=args.timeout,
        )


if __name__ == "__main__":
    main()
