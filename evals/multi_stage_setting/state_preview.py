from __future__ import annotations

from collections import Counter, defaultdict
import json
import re
from typing import Any

from evals.multi_stage_setting.contracts import EvaluationState, ScenarioGold


FACT_TYPE_LABELS = {
    "PROFILE": "프로필",
    "AGE": "나이",
    "LEVEL": "레벨",
    "STAT": "능력치",
    "SKILL": "기술",
    "ITEM": "아이템",
    "STATUS": "상태",
    "TIME": "시간",
}

FACT_KEY_LABELS = {
    "age": "나이",
    "level": "레벨",
    "profile.species": "종족",
    "profile.occupation": "직업",
    "profile.age": "나이",
    "profile.eye_color": "눈 색",
    "profile.gender": "성별",
    "profile.height": "키",
    "profile.키": "키",
    "profile.가족관계": "가족 관계",
    "stats.EXP": "경험치",
    "stats.physique": "육체",
    "stats.mental": "정신",
    "stats.supernatural": "이능",
    "stats.item_level": "아이템 레벨",
    "stats.combat_power": "종합 전투 지수",
}

WORLD_CATEGORY_LABELS = {
    "RACE": "종족",
    "FACTION": "세력",
    "LOCATION": "장소",
    "MONSTER": "몬스터",
    "POWER_SYSTEM": "능력·시스템",
    "WORLD_RULE_HISTORY": "세계 규칙·역사",
    "IMPORTANT_ITEM": "중요 아이템",
}


def render_notion_before_state(
    scenario: ScenarioGold,
    state: EvaluationState,
) -> str:
    """Render a source-derived, human-readable Notion section for one scenario.

    Stable refs and structured JSON remain in ``EvaluationState`` for scoring, but
    the preview intentionally exposes only fields useful during human Gold review.
    """

    canonical = state.canonical()
    state_hash = canonical.content_hash()
    lines = [
        "## 평가 시작 전 누적 상태 · 자동 생성",
        _preview_callout(scenario, state_hash),
    ]
    if not canonical.known_characters and not canonical.character_facts and not canonical.world_facts:
        lines.extend(
            [
                "캐릭터와 세계관 누적 상태가 없습니다.",
                "첫 회차는 빈 상태에서 평가를 시작합니다.",
            ]
        )
        return "\n".join(lines)

    character_rows = _character_rows(canonical)
    lines.append("### 캐릭터")
    if character_rows:
        lines.append(
            _notion_table(
                ("캐릭터", "분류", "특성", "현재 누적 값"),
                character_rows,
                header_column=True,
            )
        )
    else:
        lines.append("누적된 캐릭터가 없습니다.")

    lines.append("### 세계관")
    if canonical.world_facts:
        include_scope = any(item.scope_name for item in canonical.world_facts)
        headers = ["분류", "세계관 주체"]
        if include_scope:
            headers.append("범위")
        headers.extend(("설정", "현재 누적 값"))
        world_rows = []
        for item in canonical.world_facts:
            row = [
                WORLD_CATEGORY_LABELS.get(str(item.category), str(item.category)),
                item.subject_name,
            ]
            if include_scope:
                row.append(item.scope_name or "기본")
            row.extend((item.setting_name, item.value))
            world_rows.append(tuple(row))
        lines.append(_notion_table(tuple(headers), world_rows, header_column=False))
    else:
        lines.append("누적된 세계관 설정이 없습니다.")

    history_only = [
        item for item in canonical.character_history if str(item.operation) == "HISTORY_ONLY"
    ]
    if history_only:
        lines.extend(
            [
                "<details color=\"gray_bg\">",
                "<summary>현재 상태에 반영되지 않은 과거 기록</summary>",
                _notion_table(
                    ("캐릭터", "특성", "과거 값"),
                    [
                        (
                            item.entity_name,
                            _fact_key_label(item.fact_key),
                            _display_value(item.value, item.value_json),
                        )
                        for item in history_only
                    ],
                    header_column=True,
                    indent="\t",
                ),
                "</details>",
            ]
        )

    if canonical.held_world_conflicts:
        lines.append("### 검수 대기 중인 세계관 충돌")
        lines.append(
            _notion_table(
                ("세계관 주체", "설정", "충돌 후보"),
                [
                    (
                        item.subject_name,
                        item.setting_name,
                        " / ".join(item.source_values),
                    )
                    for item in canonical.held_world_conflicts
                ],
                header_column=True,
            )
        )
    return "\n".join(lines)


def _preview_callout(scenario: ScenarioGold, state_hash: str) -> str:
    review_label = "검수 전 미리보기" if str(scenario.review_status) != "FINAL" else "검수 완료 상태"
    through = scenario.cumulative_through_episode
    basis = "빈 시작 상태" if through == 0 else f"{through}화까지의 2차 Gold 적용"
    return (
        '<callout icon="⚙️" color="gray_bg">\n'
        f"\t**{review_label}** · 기준: {basis} · 상태 hash: `{state_hash[:12]}`\n"
        "\t이 영역은 누적 상태에서 자동 생성됩니다. ref·JSON 같은 채점용 필드는 숨겼으며 직접 수정하지 않습니다.\n"
        "</callout>"
    )


def _character_rows(state: EvaluationState) -> list[tuple[str, str, str, str]]:
    facts_by_ref = defaultdict(list)
    for fact in state.character_facts:
        facts_by_ref[fact.entity_ref].append(fact)

    known_by_ref = {item.entity_ref: item for item in state.known_characters}
    refs = set(known_by_ref) | set(facts_by_ref)
    ordered_refs = sorted(
        refs,
        key=lambda ref: (
            known_by_ref.get(ref) is None,
            _creation_order(known_by_ref.get(ref)),
            _entity_name(ref, known_by_ref, facts_by_ref).casefold(),
            ref,
        ),
    )
    raw_names = [_entity_name(ref, known_by_ref, facts_by_ref) for ref in ordered_refs]
    counts = Counter(raw_names)
    seen: Counter[str] = Counter()
    display_names = {}
    for ref, name in zip(ordered_refs, raw_names, strict=True):
        seen[name] += 1
        display_names[ref] = (
            f"{name} (동명이인 {seen[name]})" if counts[name] > 1 else name
        )

    rows = []
    for ref in ordered_refs:
        facts = sorted(
            facts_by_ref.get(ref, []),
            key=lambda item: (
                item.source_episode_no or 0,
                item.source_sort_order or 0,
                str(item.fact_type),
                item.fact_key,
            ),
        )
        if not facts:
            rows.append((display_names[ref], "발견", "누적 설정", "이름만 확인됨"))
            continue
        for fact in facts:
            rows.append(
                (
                    display_names[ref],
                    FACT_TYPE_LABELS.get(str(fact.fact_type), str(fact.fact_type)),
                    _fact_key_label(fact.fact_key),
                    _display_value(fact.value, fact.value_json),
                )
            )
    return rows


def _creation_order(character: Any | None) -> tuple[bool, int]:
    if character is None or character.creation_order is None:
        return True, 0
    return False, character.creation_order


def _entity_name(ref: str, known_by_ref: dict, facts_by_ref: dict) -> str:
    known = known_by_ref.get(ref)
    if known is not None:
        return known.name
    return facts_by_ref[ref][0].entity_name


def _fact_key_label(fact_key: str) -> str:
    if fact_key in FACT_KEY_LABELS:
        return FACT_KEY_LABELS[fact_key]
    leaf = fact_key.rsplit(".", 1)[-1]
    return re.sub(r"[_-]+", " ", leaf).strip() or fact_key


def _display_value(value: str | None, value_json: dict[str, Any] | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    if value_json is None:
        return "값 없음"
    return json.dumps(value_json, ensure_ascii=False, sort_keys=True)


def _notion_table(
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    *,
    header_column: bool,
    indent: str = "",
) -> str:
    header_column_value = "true" if header_column else "false"
    lines = [
        f'{indent}<table fit-page-width="true" header-row="true" '
        f'header-column="{header_column_value}">',
        f"{indent}\t<tr>",
    ]
    lines.extend(f"{indent}\t\t<td>{_escape_cell(value)}</td>" for value in headers)
    lines.append(f"{indent}\t</tr>")
    for row in rows:
        lines.append(f"{indent}\t<tr>")
        lines.extend(f"{indent}\t\t<td>{_escape_cell(value)}</td>" for value in row)
        lines.append(f"{indent}\t</tr>")
    lines.append(f"{indent}</table>")
    return "\n".join(lines)


def _escape_cell(value: str) -> str:
    # Notion table cells contain rich text, so escape Markdown/XML delimiters and
    # keep multi-line values inside one cell with explicit line breaks.
    escaped = str(value).replace("\\", "\\\\")
    for char in "*~`$[]<>{}|^":
        escaped = escaped.replace(char, f"\\{char}")
    return escaped.replace("\n", "<br>")
