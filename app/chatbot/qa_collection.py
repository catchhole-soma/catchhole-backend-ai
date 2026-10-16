"""Work-wide reads under the same execution authority as named-subject queries."""

import json

from app.chatbot.qa_render import AnswerRow, current_changed, message, safe_text, setting_label
from app.chatbot.qa_synthesis import logical_groups
from app.clients.setting_tools_client import SettingToolClientError
from app.schemas.chat_qa import SettingFocus
from app.schemas.setting_tools import SettingToolQuery


CATEGORY_LABELS = {
    "AGE": "나이",
    "LEVEL": "레벨",
    "PROFILE": "기본 정보",
    "STAT": "능력치",
    "SKILL": "스킬",
    "ITEM": "아이템",
    "STATUS": "상태",
    "TIME": "시간",
    "RACE": "종족",
    "FACTION": "세력",
    "LOCATION": "지역",
    "MONSTER": "몬스터",
    "POWER_SYSTEM": "능력 체계",
    "WORLD_RULE_HISTORY": "세계 규칙·역사",
    "IMPORTANT_ITEM": "주요 아이템",
}


class WorkLookup:
    async def revalidate_collections(self):
        for request, metadata in self.collection_checks:
            await self.query(
                request.model_copy(
                    update={
                        "page": 0,
                        "size": 1,
                        "read_token": metadata.read_token,
                    }
                )
            )

    async def collection_rows(self, collection, *, reserve=0):
        filters = [{"fact_type": collection.fact_type, "world_category": collection.world_category}]
        if collection.attribute:
            fields, stop = await self.fields(
                collection.domain,
                None,
                SettingFocus(fact_type=collection.fact_type, attribute=collection.attribute),
                reserve=reserve + 2,
                world_category=collection.world_category,
            )
            if stop:
                return stop
            filters = [
                {
                    "fact_type": f.category if collection.domain == "CHARACTER" else None,
                    "world_category": f.category if collection.domain == "WORLD" else None,
                    "setting_key": f.setting_key,
                    "scope_name": f.scope_name,
                }
                for f in fields
            ]
        if self.tool_count + 2 * len(filters) + reserve > min(
            self.cfg.max_tool_calls, self.execution.context.max_tool_calls
        ):
            return message(
                "CLARIFICATION",
                "조회할 설정 항목이 많아 값을 모두 확인하고 변경 여부를 검증하기 어려워요. 유형이나 항목을 좁혀 주세요.",
                self.codes,
            )
        self.collection_info = []
        self.collection_populations = {}
        incomplete = False
        for index, arguments in enumerate(filters):
            items, complete = await self.pages(
                f"{collection.domain}_CURRENT_COLLECTION",
                exhaustive=True,
                reserve=reserve + len(filters) + len(filters) - index - 1,
                **arguments,
            )
            incomplete |= not complete
            if (
                self.last_collection is not None
                and len(items) < self.last_collection.record_count
                and items
            ):
                # Stable subject ordering makes every prior subject complete. The
                # last subject may have a contradicting status on the next page.
                self.incomplete_subjects.add(items[-1].subject_id)
            self.incomplete_subjects.update(i.subject_id for i in items if i.value_truncated)
            if self.last_collection is not None:
                self.collection_info.append(self.last_collection)
                category = arguments.get("world_category")
                total = self.last_collection.subject_count
                if self.collection_populations.setdefault(category, total) != total:
                    raise SettingToolClientError("CONCURRENT_UPDATE")
            for item in items:
                if arguments.get("world_category") and item.category != arguments["world_category"]:
                    raise SettingToolClientError("CONTRACT_ERROR")
                self.rows.append(AnswerRow(f"R{len(self.rows) + 1}", item, collection.domain))
                self.focused_refs.add(self.rows[-1].ref)
        if len(filters) > 1 and incomplete:
            # A missing later field can change the verdict for any observed subject.
            self.incomplete_subjects.update(row.item.subject_id for row in self.rows)
        return None

    async def answer_collection(self, plan):
        try:
            result = await self._answer_collection(plan)
            await self.revalidate_collections()
            return result
        except SettingToolClientError as exc:
            if exc.code == "CONCURRENT_UPDATE" or exc.status_code == 409:
                return current_changed(self.codes)
            raise

    async def _answer_collection(self, plan):
        collection = plan.collection
        # These are counts/lists of registered subjects, not of their fact rows.
        subjects_only = (
            collection.fact_type is None
            and collection.attribute is None
            and collection.predicate is None
        )
        population = (
            "현재 등록된 인물" if collection.domain == "CHARACTER" else "세계관에 등록된 설정 주체"
        )
        if collection.world_category:
            population = f"세계관에 등록된 {CATEGORY_LABELS[collection.world_category]} 설정"
        if subjects_only:
            tool = "FIND_CHARACTERS" if collection.domain == "CHARACTER" else "FIND_WORLD_SETTINGS"
            if plan.answer_shape == "COUNT":
                result = await self.query(
                    SettingToolQuery(
                        tool=tool,
                        size=1,
                        world_category=collection.world_category,
                    )
                )
                if result.partial:
                    self.codes.append("PARTIAL_RESULTS")
                ending = "명이에요" if collection.domain == "CHARACTER" else "개예요"
                return message(
                    "ANSWER",
                    f"{population} 기준으로 {result.items.total_elements}{ending}.",
                    self.codes,
                )
            guard_request = SettingToolQuery(
                tool=f"{collection.domain}_CURRENT_COLLECTION",
                size=1,
                world_category=collection.world_category,
            )
            guard = await self.query(guard_request)
            self.collection_checks.append((guard_request, guard.collection))
            found, complete = await self.pages(
                tool,
                world_category=collection.world_category,
                exhaustive=True,
                reserve=1,
            )
            if len({item.subject_id for item in found}) != len(found) or (
                complete and len(found) != guard.collection.subject_count
            ):
                raise SettingToolClientError("CONCURRENT_UPDATE")
            count = (
                f"{len(found)}명이에요."
                if collection.domain == "CHARACTER"
                else f"{len(found)}개예요."
            )
            header = f"{population}은 {'확인한 범위에서 ' if not complete else ''}{count}"
            text = header + "\n" + "\n".join(f"- {safe_text(i.subject_name)}" for i in found)
            if len(text) > self.cfg.max_answer_characters:
                lines = text.splitlines()
                while len("\n".join(lines)) > self.cfg.max_answer_characters:
                    lines.pop()
                text = "\n".join(lines)
                self.codes.append("ANSWER_LIMIT")
            return message("ANSWER", text, self.codes)
        stop = await self.collection_rows(collection)
        if stop:
            return stop
        if not self.collection_info:
            return message(
                "INSUFFICIENT_EVIDENCE",
                "현재 등록 정보에서 질문에 해당하는 설정을 찾지 못했어요.",
                self.codes,
            )
        groups = logical_groups(self.rows, by_subject=True)
        total = sum(self.collection_populations.values())
        if collection.domain == "WORLD" and collection.attribute and not collection.world_category:
            categories = list(self.collection_populations)
            population = (
                f"세계관에 등록된 {CATEGORY_LABELS[categories[0]]} 설정"
                if len(categories) == 1
                else "세계관의 선택한 분류에 등록된 설정 주체"
            )
        if plan.answer_shape == "DISTINCT" and collection.predicate is None:
            if plan.include_evidence:
                await self.evidence(self.rows, reserve=len(self.collection_checks))
            return self.render_distinct(self.rows, population)
        if collection.predicate:
            decisions = await self.interpret_condition(groups, collection.predicate)
            selected_refs = {
                ref
                for decision in decisions
                if decision.verdict == "MATCH"
                for ref in decision.row_refs
            }
            selected_rows = [row for row in self.rows if row.ref in selected_refs]
            if plan.include_evidence:
                await self.evidence(selected_rows, reserve=len(self.collection_checks))
            if plan.answer_shape == "DISTINCT":
                if len(decisions) != len(groups) or any(d.verdict == "UNKNOWN" for d in decisions):
                    self.codes.append("CONDITION_UNCERTAIN")
                return self.render_distinct(
                    selected_rows,
                    population + " 중 조건을 확인한 대상",
                )
            return self.render_condition(
                groups,
                decisions,
                population,
                total,
                shape=plan.answer_shape,
                missing=(total - self.collection_info[0].subjects_with_values)
                if len(self.collection_info) == 1
                else (max(0, total - len(groups)) if "PARTIAL_RESULTS" not in self.codes else 0),
            )
        if plan.include_evidence:
            await self.evidence(self.rows, reserve=len(self.collection_checks))
        return await self.registered_answer(plan, self.rows)

    def render_distinct(self, rows, population):
        groups = {}
        incomplete = False
        for row in rows:
            path = (row.domain, row.item.category, row.item.scope_name, row.item.setting_key)
            _, values = groups.setdefault(path, (row.item, {}))
            try:
                # World properties are strings in the Spring contract; unlike
                # character display values, this is the canonical property value.
                value = (
                    row.item.value
                    if row.domain == "WORLD"
                    else json.loads(row.item.structured_value_json or "null")
                )
                if isinstance(value, dict):
                    value = (
                        value.get("value") if value.get("value") is not None else value.get("name")
                    )
                if value is None or isinstance(value, (dict, list)) or row.item.value_truncated:
                    incomplete = True
                    continue
                values.setdefault(json.dumps(value, ensure_ascii=False), []).append(row)
            except ValueError:
                incomplete = True
        if incomplete:
            self.codes.append("SCALAR_UNAVAILABLE")
        if not any(values for _, values in groups.values()):
            return message(
                "INSUFFICIENT_EVIDENCE",
                "해당 설정은 있지만 종류를 정확히 셀 수 있는 값을 확인하지 못했어요.",
                self.codes,
            )
        multiple = len(groups) > 1
        parts = [f"{population}의 설정 항목별로 확인한 값이에요."] if multiple else []
        sources = []
        for number, (item, values) in enumerate(groups.values(), 1):
            label = f"{CATEGORY_LABELS.get(item.category, '설정')} · {setting_label(item)}"
            header = (
                f"{number}. {label}에서 확인한 값은 {len(values)}종류예요."
                if multiple and values
                else f"{number}. {label}: 단일 값을 확인하지 못했어요."
                if multiple
                else f"{population}의 해당 속성에서 확인한 값은 {len(values)}종류예요."
            )
            lines = [(header, [])]
            for value in sorted(values):
                linked = list(
                    dict.fromkeys(ref for row in values[value] for ref, _ in row.evidence)
                )
                lines.append((f"- {safe_text(str(json.loads(value)))}", linked))
            for line, linked in lines:
                next_sources = list(dict.fromkeys([*sources, *linked]))
                if linked:
                    line += " · 근거 " + ", ".join(
                        str(next_sources.index(ref) + 1) for ref in linked
                    )
                if len("\n".join([*parts, line])) > self.cfg.max_answer_characters:
                    self.codes.append("ANSWER_LIMIT")
                    break
                parts.append(line)
                sources = next_sources
            else:
                continue
            break
        result = message("ANSWER", "\n".join(parts), self.codes)
        return result.model_copy(
            update={"result": result.result.model_copy(update={"sources": sources})}
        )
