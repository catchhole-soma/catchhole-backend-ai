"""합성 회귀: gpt-6 계열 입력 추정, 원문 검색 도구 예약, PARTIAL 2라운드 장면 확장.

제공자·임베딩·DB·원고 없이 동작 계약만 검사한다. 실제 모델 정답률 측정이 아니다.
"""

import asyncio
import math
import socket
from uuid import UUID

import pytest
import tiktoken

from app.schemas.setting_tools import SettingToolResponse
from app.usage.metering import _estimate_text_token_byte_upper_bound, estimate_text_input_tokens
from tests.test_chat_evidence_budget import answer_context
from tests.test_chat_focused_lookup import item, uid
from tests.test_chat_hybrid import replay
from tests.test_chat_hybrid_fallback import partial_case, replace_keyword_evidence


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("이 회귀는 외부 서비스에 접근하지 않는다")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def test_gpt6_family_estimates_input_with_o200k_not_utf8_bytes():
    korean = "비요른은 방패를 들고 적의 공격을 막아낸 뒤 메이스로 반격했다. 아이나르가 뒤에서 달려와 적을 쓰러뜨렸다. " * 60
    expected = math.ceil(len(tiktoken.get_encoding("o200k_base").encode(korean)) * 1.10) + 256
    for model in ("gpt-6-luna", "gpt-6.1-sol", "gpt-5.6-terra"):
        assert estimate_text_input_tokens("", korean, model) == expected
    # 한글은 UTF-8 3바이트라 byte 상한은 실제 토큰의 3배 이상이다. 알려지지 않은 이름만 그 상한을 유지한다.
    byte_bound = _estimate_text_token_byte_upper_bound("", korean, 0, "")
    assert estimate_text_input_tokens("", korean, "synthetic-unknown-model") == byte_bound
    assert expected * 2 < byte_bound


def test_source_tool_reserve_applies_only_when_source_search_is_planned():
    session, plan, _ = answer_context()
    assert plan.source_need == "PROCESS"
    assert session.source_tool_reserve(plan) == 3 * session.hcfg.max_rounds + 2
    assert session.source_tool_reserve(plan.model_copy(update={"source_need": "NONE"})) == 0


def test_row_page_reserve_shares_remaining_tools_across_unfocused_subjects():
    session, plan, _ = answer_context()
    session.execution.context.max_tool_calls = 20
    limit = session.cfg.max_tool_calls
    focused = plan.subjects[0]
    unfocused = focused.model_copy(update={"focus": None})
    three = plan.model_copy(update={"subjects": [unfocused] * 3})
    # 집중 조회·등록 전용 계획은 기존 예약(base)을 그대로 쓴다.
    assert session.row_page_reserve(plan, focused, 0, base=5) == 5
    assert session.row_page_reserve(plan.model_copy(update={"source_need": "NONE"}), unfocused, 0, base=5) == 5
    # 세 인물·원문 검색 계획: FIND 3회 뒤 여유 = 16 - 3 - 8 - 2 = 3 → 첫 인물 1쪽, 남은 둘이 나눠 쓴다.
    session.tool_count = 3
    first = session.row_page_reserve(three, unfocused, 0, base=4)
    assert limit - session.tool_count - first == 1
    session.tool_count = 4
    second = session.row_page_reserve(three, unfocused, 1, base=4)
    assert limit - session.tool_count - second == 1
    session.tool_count = 5
    third = session.row_page_reserve(three, unfocused, 2, base=4)
    assert limit - session.tool_count - third == 3
    # 어떤 경우에도 원문 검색 몫(3*라운드+2)은 남긴다.
    for reserve in (first, second, third):
        assert reserve >= session.source_tool_reserve(three)
    # 예산이 바닥나면 첫 페이지만 시도하고 추가 페이지는 읽지 않는다(reserve가 남은 예산 전체).
    session.tool_count = limit - 7
    assert session.row_page_reserve(three, unfocused, 2, base=4) == limit - session.tool_count


def test_history_pages_stop_before_the_reserved_source_tools():
    session, _, _ = answer_context()
    session.execution.context.max_tool_calls = 20
    pages_read = []

    async def fake_query(request):
        session.tool_count += 1
        pages_read.append(request.page)
        row = item(
            701,
            "세린",
            kind="HISTORY",
            category="SKILL",
            key="skill.synthetic-1",
            label="잔광",
            value=str(request.page),
            episode=request.page + 1,
            number=2000 + request.page,
        )
        return SettingToolResponse.model_validate(
            {
                "schemaVersion": 1,
                "tool": request.tool,
                "items": {
                    "content": [row],
                    "page": request.page,
                    "size": 20,
                    "totalElements": 100,
                    "totalPages": 5,
                    "hasNext": True,
                },
                "partial": False,
                "limitations": [],
            }
        )

    session.query = fake_query
    reserve = 8
    session.tool_count = session.cfg.max_tool_calls - reserve - 1
    items, complete = asyncio.run(
        session.pages("CHARACTER_HISTORY", target=UUID(str(uid(701))), reserve=reserve)
    )
    # 첫 페이지는 읽지만, 예약분(검색·이웃·영수증·재검증)을 침범하는 두 번째 페이지는 읽지 않는다.
    assert pages_read == [0]
    assert len(items) == 1 and complete is False
    assert "PARTIAL_RESULTS" in session.codes
    assert session.tool_count == session.cfg.max_tool_calls - reserve


def test_partial_round_two_reads_focus_neighbors_of_the_supported_scene(tmp_path):
    radii = []

    def setup(search):
        replace_keyword_evidence(search)
        original = search.neighbors

        async def neighbors(scope, seeds, radius, limit):
            radii.append((radius, tuple(seed.chunk_id for seed in seeds)))
            return await original(scope, seeds, radius, limit)

        search.neighbors = neighbors

    result = asyncio.run(replay(tmp_path, partial_case(), search_setup=setup))
    finished = result["finish"]["result"]
    assert finished["outcome"] == "ANSWER"
    # 2라운드: 전역 vector 후보의 기본 이웃 다음에, PARTIAL 초안이 인용한 청크의 확장 이웃을 읽는다.
    assert result["search"].calls == [
        "keyword", "neighbors", "vector", "neighbors", "neighbors", "revalidate",
    ]
    assert [radius for radius, _ in radii] == [1, 1, 2]
    keyword_ids = {chunk.chunk_id for chunk in result["search"].keyword_chunks}
    assert radii[2][1] and set(radii[2][1]) <= keyword_ids
    # 보존된 PARTIAL 답과 그 근거는 그대로 유지된다.
    assert [source["sourceEpisodeNo"] for source in finished["retrievalSources"]] == [3]
