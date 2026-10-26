"""Prepare tokenizer assets before individual tests forbid network access."""

import pytest
import tiktoken


def pytest_sessionstart(session):
    # tiktoken ships code but downloads its encoding data on first use. Preparing
    # both encodings here prevents cold-cache runs from depending on test order.
    # No provider/API request is made, and test bodies retain their network bans.
    try:
        for name in ("o200k_base", "cl100k_base"):
            tiktoken.get_encoding(name)
    except Exception as exc:
        raise pytest.UsageError(
            "Tokenizer cache preparation failed before tests. Prepare o200k_base and "
            "cl100k_base in TIKTOKEN_CACHE_DIR with network access, then rerun pytest."
        ) from exc
