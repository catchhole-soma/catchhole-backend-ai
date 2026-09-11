import asyncio
import json

import httpx
import pytest

from evals.character_status_stability.api import EvalApi, EvalApiError, validate_local_base_url

WORK = "00000000-0000-0000-0000-000000000001"
BATCH = "00000000-0000-0000-0000-000000000002"
EPISODE = "00000000-0000-0000-0000-000000000003"
CHARACTER = "00000000-0000-0000-0000-000000000004"
FACT = "00000000-0000-0000-0000-000000000005"


def ok(data):
    return httpx.Response(200, json={"success": True, "data": data})


def fail(code, status=409):
    return httpx.Response(status, json={"success": False, "error": {"code": code}})


def candidate(identifier="one", **overrides):
    return {
        "id": identifier,
        "candidateKind": "SETTING",
        "matchStatus": "MATCHED",
        "reviewStatus": "PENDING_REVIEW",
        "valueValidation": {"status": "VALID"},
        "comparisonStatus": "COMPLETED",
        "suggestedOperation": "REMOVE",
        "comparisonBaseSnapshotVersion": 7,
        **overrides,
    }


def groups_page(groups, has_next=False):
    return ok({"groups": {"content": groups, "hasNext": has_next}})


@pytest.mark.parametrize("url", [
    "https://api.catchhole.com", "http://example.com", "http://127.0.0.1.example.com",
    "http://localhost@api.catchhole.com", "http://user:secret@localhost",
    "http://localhost/api", "http://localhost?token=secret", "http://localhost#fragment",
    "http://0.0.0.0:8080", "file:///tmp/backend", "http://localhost:invalid",
])
def test_external_or_ambiguous_base_urls_rejected_before_transport(url):
    with pytest.raises(ValueError, match="localhost or loopback") as error:
        EvalApi(url)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080/"])
def test_explicit_loopback_urls_allowed(url):
    assert validate_local_base_url(url) == url.rstrip("/")


def test_local_redirect_is_never_followed_to_an_external_host():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://api.catchhole.com/api/v1/auth/login"})

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            with pytest.raises(EvalApiError) as error:
                await api.signup_or_login(email="private@example.test", password="secret123",
                                         display_name="eval", phone_number="01012345678")
            assert error.value.code == "API_REDIRECT_REJECTED"

    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0].url.host == "localhost"


def test_signup_uses_current_legal_ids_fake_phone_and_keeps_credentials_private():
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/login"):
            return fail("AUTH_INVALID_CREDENTIALS", 401)
        if path.endswith("/legal-documents/current"):
            return ok({"termsOfService": {"id": 31}, "privacyPolicy": {"id": 32}})
        if path.endswith("/phone-verifications"):
            return ok({"verificationId": "private-flow"})
        if path.endswith("/private-flow/confirm"):
            assert json.loads(request.content) == {"code": "123456"}
            return ok({"phoneVerificationToken": "private-signup-token"})
        if path.endswith("/signup"):
            payload = json.loads(request.content)
            assert payload["termsDocumentId"] == 31
            assert payload["privacyPolicyDocumentId"] == 32
            assert payload["phoneVerificationToken"] == "private-signup-token"
            return ok({"accessToken": "private-access-token"})
        assert request.headers["Authorization"] == "Bearer private-access-token"
        return ok({"id": WORK})

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            identity = await api.signup_or_login(email="private@example.test", password="secret123",
                                                display_name="eval", phone_number="01012345678")
            work = await api.create_work("evaluation")
            artifact = json.dumps({"identity": identity, "work": work})
            assert identity == {"authenticated": True, "created": True}
            assert all(value not in artifact for value in [
                "private", "secret123", "01012345678",
            ])

    asyncio.run(run())
    assert [request.method for request in requests] == ["POST", "GET", "POST", "POST", "POST", "POST"]


def test_existing_login_avoids_phone_signup_and_upload_preserves_original_bytes():
    original = "1화\n비요른의 발이 아팠다.\n".encode()

    def handler(request):
        if request.url.path.endswith("/login"):
            return ok({"accessToken": "private-token"})
        if request.url.path.endswith("/episodes"):
            assert "multipart/form-data" in request.headers["Content-Type"]
            assert original in request.content
            assert b'name="metadata"' in request.content
            assert b'name="episodeFiles"' in request.content
            return ok({"status": "COMPLETED", "episodeCount": 1, "batchId": BATCH,
                       "createdEpisodes": [{"id": EPISODE, "episodeNo": 1}]})
        assert json.loads(request.content) == {
            "jobType": "SETTING_EXTRACTION", "batchId": BATCH, "episodeId": EPISODE,
        }
        return ok([{"id": FACT, "episodeId": EPISODE}])

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            identity = await api.signup_or_login(email="eval@example.test", password="secret123",
                                                display_name="eval", phone_number="01012345678")
            assert identity["created"] is False
            upload = await api.upload_episode(WORK, 1, original)
            job = await api.create_analysis_job(WORK, upload["batchId"], EPISODE)
            assert job["episodeId"] == EPISODE

    asyncio.run(run())


def test_confirmation_is_whole_group_and_follows_actual_operations():
    rows = [
        candidate("remove"), candidate("history", suggestedOperation="HISTORY_ONLY"),
        candidate("review", suggestedOperation="REVIEW_REQUIRED"),
        candidate("add", suggestedOperation="ADD"), candidate("update", suggestedOperation="UPDATE"),
    ]
    initial = [{"groupKey": "비요른", "candidates": rows}]
    posts = []

    def handler(request):
        if request.method == "GET":
            assert request.url.params["reviewStatus"] == "PENDING_REVIEW"
            return groups_page([] if posts else initial)
        posts.append(json.loads(request.content))
        return ok({"groupKey": "비요른", "confirmed": True})

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            api._access_token = "private-token"
            result = await api.confirm_pending_groups(WORK, BATCH)
            assert result["complete"] is True
            assert result["unresolved"] == []
            assert result["pendingGroups"] == []

    asyncio.run(run())
    assert posts == [{"batchId": BATCH, "candidates": [
        {"candidateId": "remove", "applicationMode": "APPLY_PROPOSAL", "baseSnapshotVersion": 7},
        {"candidateId": "history", "applicationMode": "HISTORY_ONLY", "baseSnapshotVersion": 7},
        {"candidateId": "review", "applicationMode": "HISTORY_ONLY", "baseSnapshotVersion": 7},
        {"candidateId": "add", "applicationMode": "APPLY_PROPOSAL", "baseSnapshotVersion": 7},
        {"candidateId": "update", "applicationMode": "APPLY_PROPOSAL", "baseSnapshotVersion": 7},
    ]}]
    assert rows[2]["suggestedOperation"] == "REVIEW_REQUIRED"
    assert rows[2]["reviewStatus"] == "PENDING_REVIEW"  # The response fixture is never rewritten.


@pytest.mark.parametrize("overrides", [
    {"suggestedOperation": "UNKNOWN_NEW_OP"},
    {"comparisonStatus": "FAILED"}, {"matchStatus": "AMBIGUOUS"},
    {"valueValidation": {"status": "INVALID"}},
    {"matchStatus": "UNRESOLVED", "suggestedOperation": "REVIEW_REQUIRED"},
    {"comparisonStatus": "PENDING", "suggestedOperation": "REVIEW_REQUIRED"},
    {"comparisonStatus": "FAILED", "suggestedOperation": "REVIEW_REQUIRED"},
    {"matchStatus": "AMBIGUOUS", "suggestedOperation": "REVIEW_REQUIRED"},
    {"valueValidation": {"status": "INVALID"}, "suggestedOperation": "REVIEW_REQUIRED"},
    {"candidateKind": "CHARACTER_DISCOVERY", "comparisonStatus": "FAILED", "suggestedOperation": None},
    {"candidateKind": "CHARACTER_DISCOVERY", "suggestedOperation": "UNKNOWN_NEW_OP"},
])
def test_unresolved_unknown_or_failed_member_blocks_the_whole_group(overrides):
    group = {"groupKey": "비요른", "candidates": [candidate(), candidate("blocked", **overrides)]}

    def handler(request):
        assert request.method == "GET"  # No partial confirm or semantic override.
        return groups_page([group])

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            api._access_token = "private-token"
            result = await api.confirm_pending_groups(WORK, BATCH)
            assert result["complete"] is False
            assert result["confirmed"] == []
            assert result["unresolved"][0]["diagnostics"][0]["candidateId"] == "blocked"

    asyncio.run(run())


def test_discovery_and_new_character_settings_share_one_real_group_confirmation():
    group = {"groupKey": "비요른", "candidates": [
        candidate("discovery", candidateKind="CHARACTER_DISCOVERY", matchStatus="UNRESOLVED",
                  comparisonStatus="NOT_REQUIRED", suggestedOperation=None),
        candidate("injury", matchStatus="UNRESOLVED",
                  comparisonStatus="WAITING_FOR_CHARACTER_MATCH", suggestedOperation=None),
    ]}
    posts = []

    def handler(request):
        if request.method == "GET":
            return groups_page([] if posts else [group])
        assert request.url.path.endswith("/group-confirm")
        posts.append(json.loads(request.content))
        return ok({"groupKey": "비요른"})

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            api._access_token = "private-token"
            assert (await api.confirm_pending_groups(WORK, BATCH))["complete"]

    asyncio.run(run())
    assert len(posts) == 1
    assert [row["candidateId"] for row in posts[0]["candidates"]] == ["discovery", "injury"]


def test_stale_confirmation_returns_requeued_diagnostic_and_does_not_retry():
    posts = []

    def handler(request):
        if request.method == "GET":
            return groups_page([{"groupKey": "비요른", "candidates": [candidate(
                comparisonStatus="PENDING" if posts else "COMPLETED",
            )]}])
        posts.append(request)
        return fail("SETTING_CANDIDATE_COMPARISON_NOT_READY")

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            api._access_token = "private-token"
            result = await api.confirm_pending_groups(WORK, BATCH)
            assert not result["complete"]
            assert result["pendingGroups"][0]["candidates"][0]["comparisonStatus"] == "PENDING"
            assert result["unresolved"][0]["error"]["code"] == "SETTING_CANDIDATE_COMPARISON_NOT_READY"

    asyncio.run(run())
    assert len(posts) == 1


def test_fact_history_follows_all_pages_filters_character_and_fetches_detail():
    def handler(request):
        if request.url.path.endswith("/search"):
            assert request.url.params["scope"] == "ALL"
            if request.url.params["page"] == "0":
                return ok({"content": [{"characterId": WORK, "characterFactId": WORK}], "hasNext": True})
            return ok({"content": [{"characterId": CHARACTER, "characterFactId": FACT}], "hasNext": False})
        assert request.url.path.endswith(FACT)
        return ok({"characterFactId": FACT, "factKey": "status.발_부상",
                   "contributesToCurrentSnapshot": False, "evidenceQuotes": ["발이 나았다."]})

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            api._access_token = "private-token"
            facts = await api.get_character_facts(WORK, CHARACTER)
            assert len(facts) == 1
            assert facts[0]["evidenceQuotes"] == ["발이 나았다."]
            assert not facts[0]["contributesToCurrentSnapshot"]

    asyncio.run(run())


def test_server_failure_body_is_not_exposed_in_exception_diagnostics():
    def handler(request):
        return httpx.Response(500, json={"success": False, "error": {
            "code": "secret token", "message": "private-access-token", "details": "secret-password",
        }})

    async def run():
        async with EvalApi("http://localhost:8080", transport=httpx.MockTransport(handler)) as api:
            api._access_token = "private-token"
            with pytest.raises(EvalApiError) as error:
                await api.get_usage()
            assert error.value.to_dict() == {"code": "API_HTTP_ERROR", "statusCode": 500}
            assert "secret" not in str(error.value)

    asyncio.run(run())
