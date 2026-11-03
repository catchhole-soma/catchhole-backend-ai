"""Guards shared by every harness step: output location, loopback DB, paid opt-in, keys, prices, git identity."""

import json
import os
import re
import subprocess
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_NAME = re.compile(r"validation_experiment_[a-z0-9_]{1,40}")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# USD per 1M tokens (input, output). Cached input is shown separately and priced as input (no discount assumed).
PRICES = {
    "gpt-6.1-terra": (Decimal(1), Decimal(5)),
    "gpt-6-luna": (Decimal("0.10"), Decimal("0.50")),
    "text-embedding-3-small": (Decimal("0.02"), Decimal(0)),
}


class HarnessError(RuntimeError):
    """A refused or impossible harness step; the message never contains secrets or source text."""


def utc_stamp():
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def outside_repo(path):
    """Resolve an output path and refuse anything inside this repository (production data must stay out)."""
    resolved = Path(path).expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise HarnessError("Harness outputs must live outside the AI repository")
    return resolved


def local_db_url(url):
    """Only a loopback PostgreSQL database named validation_experiment_* may be touched."""
    parsed = make_url(url)
    if parsed.host not in LOOPBACK or not parsed.database or not DB_NAME.fullmatch(parsed.database):
        raise HarnessError("Only a loopback database named validation_experiment_* is allowed")
    if not parsed.drivername.startswith("postgresql"):
        raise HarnessError("PostgreSQL URL required")
    return url


def require_paid(args, what):
    if not getattr(args, "confirm_paid", False) and not getattr(args, "fake", False):
        raise HarnessError(f"{what} calls a paid API; pass --confirm-paid (or --fake for the offline provider)")


def api_key():
    """The provider key comes from the process environment only and is never printed or stored."""
    key = os.environ.get("LLM_API_KEY", "")
    if not key.strip():
        raise HarnessError("LLM_API_KEY is not set in the environment")
    return key


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha256(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _nfc(value):
    return unicodedata.normalize("NFC", value)


def manuscript_path(directory, episode_no, name=None):
    """`NNN화 *.txt` in the manuscripts folder (or the exact file name, when given).

    Names are compared after NFC normalization: macOS keeps Korean file names decomposed (NFD).
    """
    directory = Path(directory)
    entries = [path for path in directory.iterdir() if path.is_file()]
    if name:
        matches = [path for path in entries if _nfc(path.name) == _nfc(name)]
        if len(matches) != 1:
            raise HarnessError(f"Manuscript file not found for episode {episode_no}")
        return matches[0]
    prefix = _nfc(f"{episode_no:03d}화")
    matches = sorted(path for path in entries if _nfc(path.name).startswith(prefix) and path.suffix == ".txt")
    if len(matches) != 1:
        raise HarnessError(f"Expected exactly one manuscript for episode {episode_no}, found {len(matches)}")
    return matches[0]


def read_manuscript(path):
    """UTF-8 text exactly as stored (no newline or BOM normalization), so hashes match the bytes."""
    return Path(path).read_bytes().decode("utf-8")


def git_identity(path):
    """`{sha, dirty}` of a checkout, or None when it is not a git checkout."""
    try:
        sha = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True,
                             text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(path), "status", "--porcelain"], check=True,
                                    capture_output=True, text=True).stdout.strip())
        return {"sha": sha, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return None


def usd(model, input_tokens, output_tokens):
    prices = PRICES.get(model)
    if prices is None:
        return None
    return float((Decimal(input_tokens) * prices[0] + Decimal(output_tokens) * prices[1]) / Decimal(1_000_000))
