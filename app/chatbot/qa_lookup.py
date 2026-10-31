"""Resolve question vocabulary against observed fields before fetching values.

Both RDB answers and manuscript answers use this path. The model can select local
references, but cannot invent a storage key, subject ID, or authority scope.
"""

import unicodedata

from app.chatbot.qa_collection import CATEGORY_LABELS
from app.chatbot.qa_prompts import FIELD_PROMPT, FIELD_VERSION
from app.chatbot.qa_render import (
    AnswerRow,
    current_changed,
    message,
    render_character_list,
    safe_text,
)
from app.chatbot.qa_validation import ChatResponseContractError
from app.clients.setting_tools_client import SettingToolClientError
from app.schemas.chat_qa import FieldSelection, SettingFocus


def normalized(value):
    return unicodedata.normalize("NFC", value.strip()).casefold()


def _field_key(item):
    return item.category, item.scope_name, item.setting_key


WORLD_CATEGORY_NAME_SUFFIXES = {
    "RACE": ("종족",),
    "FACTION": ("세력",),
    "LOCATION": ("지역",),
    "MONSTER": ("몬스터",),
    "POWER_SYSTEM": ("능력 체계",),
    "WORLD_RULE_HISTORY": ("세계 규칙·역사",),
    "IMPORTANT_ITEM": ("주요 아이템", "주요 물건"),
}


def _world_name_without_category(subject):
    """Remove only a separated, explicit category label; never infer an alias."""
    if subject.domain != "WORLD":
        return None
    name = subject.name.strip()
    for suffix in WORLD_CATEGORY_NAME_SUFFIXES.get(subject.world_category, ()):
        if name.endswith(suffix):
            prefix = name[: -len(suffix)]
            if prefix and prefix[-1].isspace() and prefix.strip():
                return prefix.strip()
    return None


class FocusedLookup:
    async def resolve_subject(self, subject, *, allow_missing=False):
        """Resolve the same named-subject contract for RDB and manuscript answers."""
        tool = "FIND_CHARACTERS" if subject.domain == "CHARACTER" else "FIND_WORLD_SETTINGS"
        found, complete = await self.pages(
            tool,
            query=subject.name.strip(),
            world_category=subject.world_category,
        )
        # A registered full name always wins. Missing pages or multiple matches
        # must not be reinterpreted as a missing name, nor relax the category.
        if complete and not found:
            shorter_name = _world_name_without_category(subject)
            if shorter_name is not None:
                found, complete = await self.pages(
                    tool,
                    query=shorter_name,
                    world_category=subject.world_category,
                )
        if subject.world_category is not None and any(
            item.category != subject.world_category for item in found
        ):
            raise SettingToolClientError("CONTRACT_ERROR")
        if not complete or len(found) > 1:
            labels = []
            for item in found[:5]:
                label = safe_text(item.subject_name)
                if subject.domain == "WORLD" and item.category in CATEGORY_LABELS:
                    label += f" ({CATEGORY_LABELS[item.category]})"
                labels.append(label)
            candidates = ", ".join(labels)
            detail = (
                f"확인한 후보는 {candidates}예요. "
                if candidates
                else "후보를 모두 확인하지 못했어요. "
            )
            return None, message(
                "CLARIFICATION",
                f"'{safe_text(subject.name)}'이 어떤 대상을 뜻하는지 구분이 필요해요. "
                + detail
                + "이름이나 종류를 더 알려주세요.",
                self.codes,
            )
        if not found:
            self.lookup_gaps.add("SUBJECT_NOT_REGISTERED")
            if "NO_RELATED_SETTING" not in self.codes:
                self.codes.append("NO_RELATED_SETTING")
            if allow_missing:
                return None, None
            return None, message(
                "INSUFFICIENT_EVIDENCE",
                f"등록 정보에서 '{safe_text(subject.name)}' 대상을 찾지 못했어요. "
                "작품에 존재하지 않는다는 뜻은 아니에요.",
                self.codes,
            )
        item = found[0]
        if item.id is None or item.id != item.subject_id:
            raise SettingToolClientError("CONTRACT_ERROR")
        return item, None

    def validate_focus(self, plan):
        observed = self.observed_question_text()
        terms = [s.focus.attribute for s in plan.subjects if s.focus and s.focus.attribute]
        if any(not term.strip() or term.strip().casefold() not in observed for term in terms):
            raise ChatResponseContractError("UNOBSERVED_FOCUS_ATTRIBUTE")
        terms = []
        condition = plan.character_filter
        collection = getattr(plan, "collection", None)
        if collection:
            terms.extend(t for t in [collection.attribute, collection.predicate] if t)
        if condition:
            terms.extend([condition.attribute, condition.value])
        if any(not term.strip() or term.strip().casefold() not in observed for term in terms):
            raise ValueError("Lookup vocabulary must occur in this question or conversation")

    async def fields(
        self,
        domain,
        target,
        focus,
        *,
        history=False,
        reserve=0,
        condition=False,
        world_category=None,
        current_items=(),
    ):
        items, complete = await self.pages(
            f"{domain}_FIELDS",
            target=target,
            fact_type=focus.fact_type,
            world_category=world_category,
            source_range=history,
            exhaustive=True,
            reserve=reserve + 1,
        )
        if not complete:
            return [], message(
                "CLARIFICATION",
                "설정 항목을 모두 확인하지 못했습니다. 조회할 유형이나 항목을 좁혀 주세요.",
                self.codes,
            )
        if current_items:
            # Bounded FIELD responses contain only historical paths. Add the
            # already-read current paths, projecting metadata only in payload().
            merged = {_field_key(item): item for item in items}
            for item in current_items:
                if not item.category or not item.setting_key:
                    raise SettingToolClientError("CONTRACT_ERROR")
                merged[_field_key(item)] = item
            items = [
                merged[key] for key in sorted(merged, key=lambda key: tuple(v or "" for v in key))
            ]
        if not items:
            self.lookup_gaps.add("NO_FIELDS")
            return [], None
        # Exact unique display names require no semantic interpretation. World paths
        # with identical labels remain ambiguous, including root versus named scopes.
        exact = [
            i for i in items if normalized(i.display_name or "") == normalized(focus.attribute)
        ]
        if domain == "CHARACTER" and len(exact) == 1:
            return exact, None
        candidates = {f"F{n}": item for n, item in enumerate(items, 1)}

        def payload():
            return {
                "question": self.execution.context.question,
                **self.conversation_payload(),
                "conversationTruncated": "CONVERSATION_LIMIT" in self.codes,
                "attribute": focus.attribute,
                "singleCondition": condition,
                "fields": [
                    {
                        "fieldRef": ref,
                        "category": item.category,
                        "displayName": item.display_name,
                        "settingKey": item.setting_key,
                        "scopeName": item.scope_name,
                    }
                    for ref, item in candidates.items()
                ],
            }

        schema = self.schema(FieldSelection)
        data = self.trim_conversation_to_fit(FIELD_PROMPT, payload, schema)
        if not self.fits(FIELD_PROMPT, data, schema):
            self.codes.append("CONTEXT_LIMIT")
            return [], message(
                "CLARIFICATION",
                "설정 항목이 많아 구분이 필요합니다. 조회할 유형이나 항목을 좁혀 주세요.",
                self.codes,
            )

        def valid(selection):
            if not set(selection.field_refs) <= candidates.keys():
                raise ValueError("Unknown field reference")
            if condition and len(selection.field_refs) > 1:
                raise ValueError("An equality condition must identify one field")

        selected = await self.model(
            FieldSelection,
            FIELD_PROMPT,
            data,
            "CHAT_INTERPRETATION",
            FIELD_VERSION,
            valid,
        )
        if selected.outcome == "CLARIFICATION":
            labels = [normalized(item.display_name or "") for item in candidates.values()]
            asked = normalized(focus.attribute) if focus.attribute else None
            duplicated = asked is not None and labels.count(asked) > 1
            if condition or domain != "CHARACTER" or duplicated:
                # 조건 검색, 세계관의 같은 경로명(루트/범위), 질문한 속성과 같은 표시명이 둘 이상인 인물 항목은
                # 되묻는 편이 맞다. 질문과 무관한 항목의 표시명 중복(158개 항목 속 '거대화' 2개)은 막지 않는다.
                return [], message(
                    "CLARIFICATION",
                    "같은 이름의 항목이나 범위가 있습니다. 어떤 설정을 뜻하는지 알려주세요.",
                    self.codes,
                )
            # 항목 이름만으로 하나를 고르지 못하면 되묻는 대신 그 유형의 현재값을 읽어 선택 모델이
            # 실제 값으로 고르게 한다('주무기' ↔ 쌍검/창, 여러 요구를 한 번에 묻는 질문).
            self.lookup_gaps.add("FIELD_AMBIGUOUS")
            return [], None
        if selected.outcome == "INSUFFICIENT_EVIDENCE":
            self.lookup_gaps.add("NO_RELEVANT_FIELDS")
        return [item for ref, item in candidates.items() if ref in selected.field_refs], None

    async def subject_rows(self, subject, target, modes, *, reserve=0):
        focus = subject.focus if "focus" in subject.model_fields_set else None
        filters = [{}]
        cached_current = None
        current_check_rows = []
        guard_reserve = 0
        fallback_rows = False
        if focus:
            filters = [{"fact_type": focus.fact_type}]
            if focus.attribute:
                mixed_range = (
                    "CURRENT" in modes
                    and "HISTORY" in modes
                    and (self.source_from is not None or self.source_to is not None)
                )
                if mixed_range:
                    # Keep room for bounded field discovery, one history page,
                    # and a current-version check after field interpretation.
                    cached_current, complete = await self.pages(
                        f"{subject.domain}_CURRENT",
                        target=target,
                        fact_type=focus.fact_type,
                        exhaustive=True,
                        reserve=reserve + 3,
                    )
                    if not complete:
                        return message(
                            "CLARIFICATION",
                            "설정 항목을 모두 확인하지 못했습니다. 조회할 유형이나 항목을 좁혀 주세요.",
                            self.codes,
                        )
                    guard_reserve = 1
                gaps_before = set(self.lookup_gaps)
                fields, stop = await self.fields(
                    subject.domain,
                    target,
                    focus,
                    history=modes == ["HISTORY"] or mixed_range,
                    reserve=reserve + guard_reserve,
                    current_items=cached_current or (),
                )
                if stop:
                    return stop
                source_fallback = bool(
                    getattr(getattr(self, "plan", None), "source_fallback", False)
                )
                if (
                    not fields
                    and "NO_FIELDS" in (self.lookup_gaps - gaps_before)
                    and focus.fact_type is not None
                    and not source_fallback
                    and not mixed_range
                ):
                    # 질문의 유형 추정('몇 급'→LEVEL)이 틀려 그 유형의 항목이 없으면 모든 유형의 항목
                    # 목록에서 다시 고른다. 값은 여전히 선택된 항목만 읽는다.
                    self.codes.append("FOCUS_TYPE_WIDENED")
                    focus = focus.model_copy(update={"fact_type": None})
                    gaps_before = set(self.lookup_gaps)
                    fields, stop = await self.fields(
                        subject.domain,
                        target,
                        focus,
                        history=modes == ["HISTORY"],
                        reserve=reserve + guard_reserve,
                    )
                    if stop:
                        return stop
                if cached_current is not None:
                    selected_paths = {_field_key(item) for item in fields}
                    cached_current = [
                        item for item in cached_current if _field_key(item) in selected_paths
                    ]
                filters = [
                    {
                        "fact_type": item.category if subject.domain == "CHARACTER" else None,
                        "setting_key": item.setting_key,
                        "scope_name": item.scope_name,
                    }
                    for item in fields
                ]
                relevant_missing = bool(
                    {"NO_RELEVANT_FIELDS", "FIELD_AMBIGUOUS"} & (self.lookup_gaps - gaps_before)
                )
                # 원문 보완(sourceFallback)이 허용된 계획은 기존대로 값을 읽지 않고 원문으로 넘어간다.
                if (
                    not fields
                    and relevant_missing
                    and not source_fallback
                    and "CURRENT" in modes
                    and not mixed_range
                ):
                    # 항목은 있는데 질문 표현('몇 급 마법사')이 등록 표시명('직업')과 달라 FIELD 선택이
                    # 비면 포기하지 않고 그 유형의 현재값 전체를 읽어 선택 모델이 고르게 한다.
                    # 해당 유형의 항목 자체가 없는 경우(NO_FIELDS)는 기존처럼 값 조회 없이 끝낸다.
                    # 집중 조회가 아니므로 일반 행 상한과 선택 단계를 그대로 거친다.
                    self.codes.append("FOCUS_FALLBACK")
                    filters = [{"fact_type": focus.fact_type}]
                    fallback_rows = True
        for mode in modes:
            collected = []
            complete = True
            if mode == "CURRENT" and cached_current is not None:
                collected = cached_current
            else:
                for arguments in filters if (mode == "CURRENT" or not fallback_rows) else []:
                    items, page_complete = await self.pages(
                        f"{subject.domain}_{mode}",
                        target=target,
                        exhaustive=focus is not None and not fallback_rows,
                        reserve=reserve + guard_reserve,
                        **arguments,
                    )
                    complete = complete and page_complete
                    collected.extend(items)
            if filters and not collected and complete:
                self.lookup_gaps.add("NO_VALUES")
            if mode == "CURRENT" and not complete:
                self.incomplete_current_subjects.add((subject.domain, target))
            if mode == "HISTORY":
                self.codes.append("HISTORY_IS_NOT_A_STATE_SNAPSHOT")
                # Each field is already server ordered. Merge different fields by source
                # episode, keeping stable server order within a field and episode.
                collected.sort(
                    key=lambda i: (i.source_episode_no is None, i.source_episode_no or 0)
                )
            for item in collected:
                if (focus is None or fallback_rows) and len(self.rows) >= self.cfg.max_items:
                    self.codes.append("PARTIAL_RESULTS")
                    break
                row = AnswerRow(f"R{len(self.rows) + 1}", item, subject.domain)
                self.rows.append(row)
                if mode == "CURRENT" and cached_current is not None:
                    current_check_rows.append(row)
                if focus is not None and not fallback_rows:
                    self.focused_refs.add(self.rows[-1].ref)
        # Cached rows were fetched before the FIELD model call. Protect FACTS
        # rendering too; summary/hybrid synthesis keeps its later final check.
        if current_check_rows and not await self.current_subjects_unchanged(current_check_rows):
            return current_changed(self.codes)
        return None

    async def character_list(self, plan):
        condition = plan.character_filter
        focus = SettingFocus(fact_type=condition.fact_type, attribute=condition.attribute)
        fields, stop = await self.fields("CHARACTER", None, focus, condition=True)
        if stop:
            return stop
        if not fields:
            return message(
                "INSUFFICIENT_EVIDENCE",
                "현재 등록된 인물 설정에서 조회 조건에 해당하는 항목을 찾지 못했습니다.",
                self.codes + ["NO_RELATED_SETTING"],
            )
        if len(fields) != 1:
            raise SettingToolClientError("CONTRACT_ERROR")
        field = fields[0]
        found, _ = await self.pages(
            "FIND_CHARACTERS",
            fact_type=field.category,
            setting_key=field.setting_key,
            value_equals=condition.value.strip(),
            exhaustive=True,
        )
        return render_character_list(
            found,
            field.display_name or condition.attribute,
            condition.value,
            self.codes,
            maximum=self.cfg.max_answer_characters,
        )
