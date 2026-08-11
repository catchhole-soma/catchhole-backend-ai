import asyncio
from types import SimpleNamespace

import scripts.smoke_openai_extraction as smoke_runner


def test_smoke_runner_awaits_extraction(monkeypatch, capsys) -> None:
    calls = []

    class FakeExtractor:
        async def extract_from_chunk(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(model_dump=lambda **_: {"candidates": []})

    monkeypatch.setattr(smoke_runner, "CharacterSettingExtractor", FakeExtractor)

    asyncio.run(smoke_runner.run_smoke_openai_extraction())

    assert len(calls) == 1
    assert calls[0]["schema_hints"] == smoke_runner.SMOKE_SCHEMA_HINTS
    assert '"candidates": []' in capsys.readouterr().out
