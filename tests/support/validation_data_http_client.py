"""Synthetic Java↔Python HTTP contract probe. No provider, extraction or operational DB."""

import asyncio
import sys
from uuid import UUID

import httpx

from app.validation.contracts import IncompleteInspection
from app.validation.data import ValidationContext, ValidationDataClient


async def main():
    base, member, work, report, baseline, target = sys.argv[1:]
    assert base.startswith("http://127.0.0.1:")
    context = ValidationContext(int(member), UUID(work), UUID(report), baseline, target)
    async with httpx.AsyncClient() as http:
        probe = await http.post(
            f"{base}/api/internal/v1/works/{work}/validation-reports/{report}/data",
            headers={
                "X-Internal-Api-Key": "gh306-synthetic-internal",
                "X-Validation-Member-Id": member,
            },
            json={
                "expectedBaselineHash": baseline,
                "expectedTargetHash": target,
                "operation": "INPUT",
                "offset": 0,
                "limit": 100,
            },
        )
        assert probe.json()["data"].get("formatVersion") == 1, str(list(probe.json()["data"]))
        client = ValidationDataClient(base, "gh306-synthetic-internal", context, http)
        frozen = await client.read_input()
        assert frozen.target.ref == "T1" and len(frozen.sources) == 1
        assert frozen.sources[0].metadata["episodeNo"] == 1
        source = await client.read_source(frozen.target)
        assert source.text == "😀민수는 검을 쥐었다. 민수는 검을 쥐었다."
        assert (await client.read_source(frozen.target, 1, 3)).text == "민수"
        entries, next_offset, more = await client.read_entries_page(0, 100)
        assert entries[0]["entryRef"] == "B1" and next_offset == 1 and not more
        await client.validate()
        outsider = ValidationDataClient(
            base,
            "gh306-synthetic-internal",
            ValidationContext(int(member) + 999, UUID(work), UUID(report), baseline, target),
            http,
        )
        try:
            await outsider.read_input()
        except IncompleteInspection:
            pass
        else:
            raise AssertionError("foreign owner accepted")
    print("GH306 actual Java-Python HTTP contract passed")


if __name__ == "__main__":
    asyncio.run(main())
