"""Local Java API boundary for the sequential character STATUS evaluation.

Only public Java APIs mutate user data. Tokens and signup credentials never
appear in returned artifact values or diagnostic exception messages.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class EvalApiError(RuntimeError):
    """A response diagnostic containing no response body or credentials."""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(f"{code} (HTTP {status_code})")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "statusCode": self.status_code}


def validate_local_base_url(base_url: str) -> str:
    """Accept localhost/loopback URLs only, without userinfo, path or query."""
    try:
        url = urlsplit(base_url)
        host = url.hostname
        _ = url.port  # Reject malformed ports before creating a client.
        loopback = host == "localhost"
        if host and not loopback:
            loopback = ipaddress.ip_address(host).is_loopback
        valid = (
            url.scheme in {"http", "https"}
            and loopback
            and url.username is None
            and url.password is None
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Evaluation API must use a localhost or loopback base URL")
    return base_url.rstrip("/")


def _id(value: str | UUID) -> str:
    return str(UUID(str(value)))


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvalApiError("RESPONSE_FORMAT")
    return value


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise EvalApiError("RESPONSE_FORMAT")
    return value


class EvalApi:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = validate_local_base_url(base_url)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._access_token: str | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._access_token = None
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Any:
        if not path.startswith("/api/v1/"):
            raise ValueError("Only relative public API paths are supported")
        headers = {"Accept": "application/json"}
        if auth:
            if self._access_token is None:
                raise EvalApiError("NOT_AUTHENTICATED")
            headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise EvalApiError("API_TRANSPORT_ERROR") from None
        if 300 <= response.status_code < 400:
            raise EvalApiError("API_REDIRECT_REJECTED", response.status_code)
        try:
            envelope = response.json()
        except ValueError:
            raise EvalApiError("RESPONSE_FORMAT", response.status_code) from None
        if not isinstance(envelope, dict):
            raise EvalApiError("RESPONSE_FORMAT", response.status_code)
        if not response.is_success or envelope.get("success") is not True:
            error = envelope.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z_0-9]{0,99}", code):
                code = "API_HTTP_ERROR"
            raise EvalApiError(code, response.status_code)
        if "data" not in envelope:
            raise EvalApiError("RESPONSE_FORMAT", response.status_code)
        return envelope["data"]

    def _accept_token(self, payload: Any) -> None:
        token = _object(payload).get("accessToken")
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise EvalApiError("AUTH_RESPONSE_FORMAT")
        self._access_token = token

    async def signup_or_login(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        phone_number: str,
    ) -> dict[str, bool]:
        try:
            payload = await self._request(
                "POST", "/api/v1/auth/login", auth=False,
                json={"email": email, "password": password},
            )
        except EvalApiError as error:
            if error.code != "AUTH_INVALID_CREDENTIALS":
                raise
        else:
            self._accept_token(payload)
            return {"authenticated": True, "created": False}

        legal = _object(await self._request("GET", "/api/v1/legal-documents/current", auth=False))
        verification = _object(await self._request(
            "POST", "/api/v1/auth/phone-verifications", auth=False,
            json={"phoneNumber": phone_number},
        ))
        verification_id = verification.get("verificationId")
        if not isinstance(verification_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", verification_id):
            raise EvalApiError("AUTH_RESPONSE_FORMAT")
        confirmed = _object(await self._request(
            "POST", f"/api/v1/auth/phone-verifications/{verification_id}/confirm", auth=False,
            json={"code": "123456"},
        ))
        payload = await self._request(
            "POST", "/api/v1/auth/signup", auth=False,
            json={
                "email": email,
                "password": password,
                "displayName": display_name,
                "termsAccepted": True,
                "privacyPolicyAcknowledged": True,
                "age14OrOlderConfirmed": True,
                "termsDocumentId": _object(legal.get("termsOfService"))["id"],
                "privacyPolicyDocumentId": _object(legal.get("privacyPolicy"))["id"],
                "phoneVerificationToken": confirmed["phoneVerificationToken"],
            },
        )
        self._accept_token(payload)
        return {"authenticated": True, "created": True}

    async def get_usage(self) -> dict[str, Any]:
        return _object(await self._request("GET", "/api/v1/ai-token-usages/me"))

    async def create_work(self, title: str) -> dict[str, Any]:
        return _object(await self._request(
            "POST", "/api/v1/works",
            json={"title": title, "genre": "판타지", "description": "순차 STATUS 평가"},
        ))

    async def upload_episode(
        self, work_id: str, episode_no: int, text: str | bytes, filename: str = "episode.txt",
    ) -> dict[str, Any]:
        metadata = {"uploadType": "SINGLE_EPISODE", "singleEpisodeNo": episode_no}
        body = text.encode("utf-8") if isinstance(text, str) else text
        result = _object(await self._request(
            "POST", f"/api/v1/works/{_id(work_id)}/episodes",
            files=[
                ("metadata", ("metadata.json", json.dumps(metadata), "application/json")),
                ("episodeFiles", (filename, body, "text/plain")),
            ],
        ))
        episodes = _rows(result.get("createdEpisodes"))
        if (
            result.get("status") != "COMPLETED" or result.get("episodeCount") != 1
            or len(episodes) != 1 or episodes[0].get("episodeNo") != episode_no
        ):
            raise EvalApiError("UPLOAD_RESPONSE_MISMATCH")
        return result

    async def create_analysis_job(
        self, work_id: str, batch_id: str, episode_id: str,
    ) -> dict[str, Any]:
        result = _rows(await self._request(
            "POST", f"/api/v1/works/{_id(work_id)}/analysis-jobs",
            json={"jobType": "SETTING_EXTRACTION", "batchId": _id(batch_id),
                  "episodeId": _id(episode_id)},
        ))
        if len(result) != 1 or result[0].get("episodeId") != str(episode_id):
            raise EvalApiError("ANALYSIS_JOB_RESPONSE_MISMATCH")
        return result[0]

    async def get_job(self, work_id: str, job_id: str) -> dict[str, Any]:
        return _object(await self._request(
            "GET", f"/api/v1/works/{_id(work_id)}/analysis-jobs/{_id(job_id)}",
        ))

    async def _pages(
        self, path: str, *, params: dict[str, Any], page_key: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index in range(1000):
            data = _object(await self._request("GET", path, params={**params, "page": index}))
            page = _object(data.get(page_key)) if page_key else data
            rows.extend(_rows(page.get("content")))
            if page.get("hasNext") is False:
                return rows
            if page.get("hasNext") is not True:
                raise EvalApiError("PAGE_RESPONSE_FORMAT")
        raise EvalApiError("PAGINATION_LIMIT")

    async def get_candidate_groups(
        self, work_id: str, batch_id: str, *, review_status: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "batchId": _id(batch_id), "size": 100, "includeLegacyCandidates": "false",
        }
        if review_status:
            params["reviewStatus"] = review_status
        return await self._pages(
            f"/api/v1/works/{_id(work_id)}/setting-candidates", params=params, page_key="groups",
        )

    @staticmethod
    def _confirmation_decisions(group: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        decisions, diagnostics = [], []
        for row in _rows(group.get("candidates")):
            reason = None
            mode = "APPLY_PROPOSAL"
            validation = row.get("valueValidation") or {}
            if row.get("reviewStatus") != "PENDING_REVIEW":
                reason = "CANDIDATE_NOT_PENDING"
            elif row.get("matchStatus") == "AMBIGUOUS":
                reason = "CHARACTER_MATCH_REQUIRED"
            elif validation.get("status") == "INVALID":
                reason = "CANDIDATE_VALUE_INVALID"
            elif row.get("suggestedOperation") not in {
                None, "ADD", "UPDATE", "MERGE", "REMOVE", "HISTORY_ONLY", "REVIEW_REQUIRED", "EXCLUDE",
            }:
                reason = "PROPOSAL_REQUIRES_REVIEW"
            elif row.get("comparisonStatus") == "FAILED":
                reason = "COMPARISON_NOT_COMPLETED"
            elif row.get("candidateKind") == "CHARACTER_DISCOVERY":
                pass  # The real group API creates the character alongside its setting rows.
            elif row.get("matchStatus") == "UNRESOLVED":
                if (
                    row.get("suggestedOperation") is not None
                    or row.get("comparisonStatus") not in {
                        "WAITING_FOR_CHARACTER_MATCH", "NOT_REQUIRED",
                    }
                ):
                    reason = "NEW_CHARACTER_REQUIRES_REVIEW"
            elif row.get("matchStatus") not in {"MATCHED", "AUTO_MATCHED_BY_NAME"}:
                reason = "CHARACTER_MATCH_REQUIRED"
            elif row.get("comparisonStatus") != "COMPLETED":
                reason = "COMPARISON_NOT_COMPLETED"
            elif row.get("suggestedOperation") in {"HISTORY_ONLY", "REVIEW_REQUIRED"}:
                # The UI defaults a completed REVIEW_REQUIRED to history, never
                # to a guessed current-state mutation or a partial group request.
                mode = "HISTORY_ONLY"
            elif row.get("suggestedOperation") not in {"ADD", "UPDATE", "MERGE", "REMOVE"}:
                reason = "PROPOSAL_REQUIRES_REVIEW"
            if reason:
                diagnostics.append({"candidateId": row.get("id"), "reason": reason,
                                    "comparisonStatus": row.get("comparisonStatus"),
                                    "suggestedOperation": row.get("suggestedOperation")})
            else:
                decisions.append({"candidateId": row["id"], "applicationMode": mode,
                                  "baseSnapshotVersion": row.get("comparisonBaseSnapshotVersion")})
        return decisions, diagnostics

    async def confirm_pending_groups(self, work_id: str, batch_id: str) -> dict[str, Any]:
        """Confirm complete groups using the frontend's supported default modes.

        Completed REVIEW_REQUIRED rows are confirmed as HISTORY_ONLY. Ambiguous
        identities, invalid values, failed comparisons, and unknown operations stay pending.
        Re-fetch complete group membership before every mutation. A stale/recompare
        response is diagnostic: this method does not drain hidden jobs or retry it.
        """
        result: dict[str, Any] = {"confirmed": [], "unresolved": []}
        attempted: set[str] = set()
        while True:
            groups = await self.get_candidate_groups(work_id, batch_id, review_status="PENDING_REVIEW")
            remaining = [group for group in groups if group["groupKey"] not in attempted]
            if not remaining:
                result.update(pendingGroups=groups, complete=not groups)
                return result
            group = remaining[0]
            group_key = group["groupKey"]
            attempted.add(group_key)
            decisions, diagnostics = self._confirmation_decisions(group)
            if diagnostics:
                result["unresolved"].append({"groupKey": group_key, "diagnostics": diagnostics})
                continue
            request = {"batchId": _id(batch_id), "candidates": decisions}
            try:
                response = await self._request(
                    "POST", f"/api/v1/works/{_id(work_id)}/setting-candidates/group-confirm",
                    json=request,
                )
            except EvalApiError as error:
                result["unresolved"].append({"groupKey": group_key, "error": error.to_dict(),
                                             "request": request})
            else:
                result["confirmed"].append({"groupKey": group_key, "request": request,
                                            "response": response})

    async def get_characters(self, work_id: str) -> list[dict[str, Any]]:
        return await self._pages(f"/api/v1/works/{_id(work_id)}/characters", params={"size": 24})

    async def get_character_snapshot(self, work_id: str, character_id: str) -> dict[str, Any]:
        return _object(await self._request(
            "GET", f"/api/v1/works/{_id(work_id)}/characters/{_id(character_id)}",
        ))

    async def get_character_facts(self, work_id: str, character_id: str) -> list[dict[str, Any]]:
        """Return all searchable Fact history with detail/evidence; PROFILE is not searchable."""
        path = f"/api/v1/works/{_id(work_id)}/character-facts"
        rows = await self._pages(path + "/search", params={"scope": "ALL", "size": 100})
        return [
            _object(await self._request("GET", f"{path}/{_id(row['characterFactId'])}"))
            for row in rows if row.get("characterId") == str(character_id)
        ]
