"""Reproducible, local-only experiment identities and JSON artifacts."""

import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def code_identity() -> dict:
    paths = sorted([*PACKAGE.rglob("*.py"), *PACKAGE.rglob("*.ttl"), ROOT / "pyproject.toml"])
    hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "codeHash": digest(hashes),
        "ruleHash": digest(
            {
                key: value
                for key, value in hashes.items()
                if key.endswith("python_rules.py") or "/owl/" in key
            }
        ),
        "fileHashes": hashes,
        "versions": {
            package: importlib.metadata.version(package)
            for package in ("pydantic", "rdflib", "owlrl")
        },
    }


def counts(result: dict) -> dict:
    return {
        "direct": len(result["directRelations"]),
        "derived": len(result["derivedRelations"]),
        "proofs": sum(len(row["proofs"]) for row in result["derivedRelations"]),
        "newRelations": sum(
            row["derivationType"] == "NEW_RELATION" for row in result["derivedRelations"]
        ),
        "withheld": len(result["withheldAssertions"]),
        "excludedDerived": len(result["excludedDerivedRelations"]),
    }
