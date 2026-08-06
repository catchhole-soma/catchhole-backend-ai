from collections.abc import Sequence

from evals.setting_extraction.models import (
    CharacterSettingSchemaSnapshot,
    PredictionBundle,
)


def canonicalize_prediction_fact_keys(
    bundle: PredictionBundle,
    schemas: Sequence[CharacterSettingSchemaSnapshot],
) -> PredictionBundle:
    """운영 resolver와 같은 exact→alias 우선순위로 예측 key를 canonicalize한다."""

    episodes = []
    for episode in bundle.episodes:
        candidates = []
        for candidate in episode.candidates:
            canonical_key = _resolve_canonical_key(candidate.attribute_name, schemas)
            candidates.append(
                candidate.model_copy(update={"canonical_attribute_name": canonical_key})
            )
        episodes.append(episode.model_copy(update={"candidates": candidates}))
    return bundle.model_copy(update={"episodes": episodes})


def _resolve_canonical_key(
    attribute_name: str,
    schemas: Sequence[CharacterSettingSchemaSnapshot],
) -> str:
    trimmed_name = attribute_name.strip()

    exact_matches = [schema for schema in schemas if schema.schema_key.strip() == trimmed_name]
    if len(exact_matches) == 1:
        return exact_matches[0].schema_key.strip()
    if len(exact_matches) > 1:
        return trimmed_name

    alias_matches = [schema for schema in schemas if _matches_alias(schema, trimmed_name)]
    if len(alias_matches) == 1:
        return alias_matches[0].schema_key.strip()

    # pattern key는 개별 속성을 구분하는 suffix가 canonical 값이므로 원래 key를 유지한다.
    return trimmed_name


def _matches_alias(schema: CharacterSettingSchemaSnapshot, attribute_name: str) -> bool:
    schema_key = schema.schema_key.strip()
    namespace = schema_key.rsplit(".", maxsplit=1)[0] + "." if "." in schema_key else ""
    for raw_alias in schema.aliases:
        alias = raw_alias.strip()
        if not alias or "." in alias:
            continue
        if attribute_name == alias or (namespace and attribute_name == namespace + alias):
            return True
    return False
