"""Explicit synthetic claim input, never a production missing-carrier fallback."""


def semantic_current_fixture(context, known, provisional=(), world_subjects=()):
    def data(value):
        return value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value

    ctx = data(context)
    characters = []
    for raw in [*known, *provisional]:
        item = data(raw)
        characters.append({
            "characterId": item.get("characterId"),
            "provisionalSubjectKey": item.get("provisionalSubjectKey"),
            "currentSlots": [{
                "factType": "STATUS", "factKey": status["factKey"],
                "factValue": status.get("factValue"), "valueJson": status.get("valueJson"),
            } for status in item.get("activeStatuses", [])],
        })
    return {
        "formatVersion": 1, "complete": True,
        **{key: ctx[key] for key in ("runId", "generation", "inputStateHash", "sourceHash")},
        "characters": characters, "worldSubjects": list(world_subjects),
    }
