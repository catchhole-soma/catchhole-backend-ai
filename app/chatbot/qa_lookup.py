"""Resolve question vocabulary against observed fields before fetching values.

Both RDB answers and manuscript answers use this path. The model can select local
references, but cannot invent a storage key, subject ID, or authority scope.
"""

import unicodedata

from app.chatbot.qa_prompts import FIELD_PROMPT, FIELD_VERSION
from app.chatbot.qa_render import AnswerRow, message, render_character_list
from app.clients.setting_tools_client import SettingToolClientError
from app.schemas.chat_qa import FieldSelection, SettingFocus


def normalized(value):
    return unicodedata.normalize("NFC", value.strip()).casefold()


class FocusedLookup:
    def validate_focus(self, plan):
        observed = self.observed_question_text()
        terms = [s.focus.attribute for s in plan.subjects if s.focus and s.focus.attribute]
        condition = plan.character_filter
        if condition:
            terms.extend([condition.attribute, condition.value])
        if any(not term.strip() or term.strip().casefold() not in observed for term in terms):
            raise ValueError("Lookup vocabulary must occur in this question or conversation")

    async def fields(self, domain, target, focus, *, history=False, reserve=0, condition=False):
        items, complete = await self.pages(
            f"{domain}_FIELDS",
            target=target,
            fact_type=focus.fact_type,
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
        if not items:
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
            return [], message(
                "CLARIFICATION",
                "같은 이름의 항목이나 범위가 있습니다. 어떤 설정을 뜻하는지 알려주세요.",
                self.codes,
            )
        return [item for ref, item in candidates.items() if ref in selected.field_refs], None

    async def subject_rows(self, subject, target, modes, *, reserve=0):
        focus = subject.focus if "focus" in subject.model_fields_set else None
        filters = [{}]
        if focus:
            filters = [{"fact_type": focus.fact_type}]
            if focus.attribute:
                fields, stop = await self.fields(
                    subject.domain,
                    target,
                    focus,
                    history=modes == ["HISTORY"],
                    reserve=reserve,
                )
                if stop:
                    return stop
                filters = [
                    {
                        "fact_type": item.category if subject.domain == "CHARACTER" else None,
                        "setting_key": item.setting_key,
                        "scope_name": item.scope_name,
                    }
                    for item in fields
                ]
        for mode in modes:
            collected = []
            for arguments in filters:
                items, _ = await self.pages(
                    f"{subject.domain}_{mode}",
                    target=target,
                    exhaustive=focus is not None,
                    reserve=reserve,
                    **arguments,
                )
                collected.extend(items)
            if mode == "HISTORY":
                self.codes.append("HISTORY_IS_NOT_A_STATE_SNAPSHOT")
                # Each field is already server ordered. Merge different fields by source
                # episode, keeping stable server order within a field and episode.
                collected.sort(
                    key=lambda i: (i.source_episode_no is None, i.source_episode_no or 0)
                )
            for item in collected:
                if focus is None and len(self.rows) >= self.cfg.max_items:
                    self.codes.append("PARTIAL_RESULTS")
                    break
                self.rows.append(AnswerRow(f"R{len(self.rows) + 1}", item, subject.domain))
                if focus is not None:
                    self.focused_refs.add(self.rows[-1].ref)
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
