"""Run independent models first, then evaluate saved outputs against an oracle."""

import argparse
import importlib.metadata
import json
import platform
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from .input import load_document

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "tests/fixtures/relationship_semantics_cases.json"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def code_identity() -> dict:
    source_hash = sha256()
    for path in sorted(Path(__file__).parent.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".ttl", ".rq"}:
            source_hash.update(str(path.relative_to(ROOT)).encode() + b"\0")
            source_hash.update(path.read_bytes() + b"\0")
    source_hash.update((ROOT / "pyproject.toml").read_bytes())

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    try:
        return {
            "gitSha": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain")),
            "sourceTreeSha256": source_hash.hexdigest(),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"gitSha": None, "dirty": None, "sourceTreeSha256": source_hash.hexdigest()}


def run(
    input_path: Path, output: Path, *, oracle_path: Path | None = None, extended: bool = False
) -> dict:
    from .rdf_model import RdfModel
    from .typed_model import TypedModel

    input_bytes = input_path.read_bytes()
    dataset = load_document(json.loads(input_bytes))
    # The oracle is not opened below until both independent outputs have been saved.
    normalized = dataset.to_dict()
    digest = sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "inputs.json", normalized)
    metadata = {
        **code_identity(),
        "inputSha256": digest,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "extended": extended,
        "dependencies": {
            name: importlib.metadata.version(name) for name in ("pydantic", "rdflib", "pyshacl")
        },
        "scope": "OFFLINE_CURATED_SNAPSHOTS_NOT_PRODUCTION_OR_LLM_EVALUATION",
    }
    models = {}
    for name, factory in (("B", TypedModel), ("C", RdfModel)):
        validations, results, roundtrips = [], [], []
        for index, case in enumerate(dataset.cases):
            model = factory(case, extended=extended)
            validation = model.validate()
            validations.append({"caseId": case.case_id, **validation})
            if not validation["conforms"]:
                continue
            restored = model.to_case()
            roundtrips.append(
                {"caseId": case.case_id, "passed": restored.to_dict() == case.to_dict()}
            )
            results.extend(model.query(query) for query in case.queries)
            if name == "C":
                # Numeric file names avoid turning supplied case IDs into paths.
                (output / f"case-{index + 1:02d}.ttl").write_text(
                    model.serialize_turtle(), encoding="utf-8"
                )
        artifact = {
            "metadata": {**metadata, "implementation": name},
            "validations": validations,
            "roundtrips": roundtrips,
            "results": results,
        }
        write_json(output / f"actual-{name}.json", artifact)
        models[name] = artifact
    from .evaluate import evaluate_results

    evaluations = {}
    if oracle_path is not None:
        oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
        if load_document(oracle).to_dict() != normalized:
            raise ValueError("oracle inputs differ from executed inputs")
        for name in models:
            # Read the already-written result, not a result that the oracle can influence.
            saved = json.loads((output / f"actual-{name}.json").read_text(encoding="utf-8"))
            evaluations[name] = evaluate_results(oracle, saved["results"])
        write_json(output / "evaluation.json", evaluations)
    summary = {
        "metadata": metadata,
        "caseCount": len(dataset.cases),
        "queryCount": sum(len(case.queries) for case in dataset.cases),
        "oracleEvaluated": oracle_path is not None,
        "oracleSha256": sha256(oracle_path.read_bytes()).hexdigest() if oracle_path else None,
        "valid": all(row["conforms"] for model in models.values() for row in model["validations"]),
        "roundtripPassed": all(
            row["passed"] for model in models.values() for row in model["roundtrips"]
        ),
        "resultsEqual": models["B"]["results"] == models["C"]["results"],
        "oraclePassed": all(result["passed"] for result in evaluations.values())
        if evaluations
        else None,
    }
    write_json(output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--oracle",
        type=Path,
        default=None,
        help="handwritten oracle; omitted means execution without scoring",
    )
    parser.add_argument("--output", type=Path, required=True, help="new artifact directory")
    parser.add_argument("--extended", action="store_true")
    args = parser.parse_args()
    summary = run(args.input, args.output, oracle_path=args.oracle, extended=args.extended)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return (
        0
        if (
            summary["valid"]
            and summary["roundtripPassed"]
            and summary["resultsEqual"]
            and summary["oraclePassed"] is not False
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
