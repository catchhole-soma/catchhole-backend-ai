"""Rebuild the invented live evaluation inputs without customer data or an API call."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from tests.test_book_character_context import inputs


def synthetic_cases():
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1] / "evals/fixtures/setting-book-context-live.json"
        ).read_text()
    )
    schemas = fixture["schema_hints"]
    cases = []
    for spec in fixture["cases"]:
        cid, label, body, gold = spec["case"], spec["label"], spec["body"], spec["gold"]
        if "names" in spec:
            names = tuple(spec["names"])
        else:
            names = ("아린 벨", "아린 남", "보라 남") if cid in ("S3", "S12") else ("아린 벨", "보라 남")
        names += tuple(f"합성인물{i:03d}" for i in range(30))
        # Identity hints (title/kinship/spouse) live in PROFILE slots like production CURRENT.
        profile = {int(index): slots for index, slots in spec.get("profile", {}).items()}
        ordered, semantic, book = inputs(names, aliases={0: ["청람"]}, profile=profile)
        sem = json.loads(semantic.character_prompt_json())
        known = [k.model_dump(mode="json", by_alias=True) for k in ordered.known_characters]
        if cid == "S9":
            sem["characters"][0]["current_slots"].append(
                {
                    "fact_type": "PROFILE",
                    "fact_key": "profile.mother",
                    "fact_value": "보라 남",
                    "value_json": {"value": "보라 남"},
                }
            )
            sem["characters"][1]["current_slots"][1].update(
                fact_key="status.하반신_마비",
                fact_value="걷지 못함",
                value_json={"active": True, "name": "하반신_마비", "description": "걷지 못함"},
            )
            known[1]["activeStatuses"][0].update(
                factKey="status.하반신_마비", factValue="걷지 못함"
            )
        cases.append(
            {
                "case": cid,
                "label": label,
                "body": body,
                "context": ordered.analysis_context.model_dump(mode="json", by_alias=True),
                "known": known,
                "semantic": {
                    "characters": json.dumps(
                        sem, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                    "worlds": semantic.world_prompt_json(),
                },
                "book": book.model_dump(mode="json", by_alias=True),
                "source_chunk_id": str(UUID(int=8)),
                "schema_hints": schemas,
                "gold": gold,
                **{key: spec[key] for key in ("expected_selection", "expected_unmatched") if key in spec},
            }
        )
    return cases


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.write_text(json.dumps(synthetic_cases(), ensure_ascii=False, indent=2) + "\n")
