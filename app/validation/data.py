"""Authenticated Spring frozen-input connector. No extraction jobs or live tools."""

import re
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

import httpx

from app.validation.contracts import IncompleteInspection


@dataclass(frozen=True)
class ValidationContext:
    member_id: int
    work_id: UUID
    report_id: UUID
    baseline_hash: str
    target_hash: str
    baseline_revision: int = 1
    job_id: UUID | None = None
    lease_token: UUID | None = field(default=None, repr=False)
    manifest_hash: str | None = None
    report_revision: int | None = None
    target_revision: int | None = None
    generation: int | None = None
    input_digest: str | None = None

    def __post_init__(self):
        if (type(self.member_id) is not int or self.member_id < 1
                or not isinstance(self.work_id, UUID) or not isinstance(self.report_id, UUID)
                or any(not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in (self.baseline_hash, self.target_hash))):
            raise ValueError("Trusted validation context is required")
        if (type(self.baseline_revision) is not int or self.baseline_revision < 1
                or (self.job_id is None) != (self.lease_token is None)
                or self.job_id is not None and (not isinstance(self.job_id, UUID)
                    or not isinstance(self.lease_token, UUID) or self.manifest_hash is None)):
            raise ValueError("Trusted execution context is required")


@dataclass(frozen=True)
class SourceDocument:
    ref: str
    kind: str
    content_hash: str
    source_version: str | None
    metadata: dict = field(repr=False)

    @classmethod
    def from_wire(cls, data, *, target=False):
        ref, kind = data["sourceRef"], data["sourceKind"]
        if (not re.fullmatch("T1" if target else r"R[1-9][0-9]*", ref)
                or kind not in {"EPISODE", "SETTING_BOOK"}
                or not re.fullmatch(r"[a-f0-9]{64}", data["contentHash"])):
            raise ValueError("Invalid source manifest")
        UUID(data["episodeId"] if kind == "EPISODE" else data["revisionId"])
        if kind == "EPISODE" and (type(data["episodeNo"]) is not int or data["episodeNo"] < 1):
            raise ValueError("Invalid episode source")
        if kind == "SETTING_BOOK":
            UUID(data["settingBookId"])
            if (type(data["revisionNo"]) is not int or data["revisionNo"] < 1
                    or data["referenceTimeKind"] not in {"UNSPECIFIED", "WORK_START", "EPISODE"}):
                raise ValueError("Invalid setting-book revision")
        return cls(ref, kind, data["contentHash"], data.get("contentS3Version"), dict(data))


@dataclass(frozen=True)
class FrozenInput:
    context: ValidationContext
    target: SourceDocument
    sources: tuple[SourceDocument, ...]
    manifest_hash: str
    entry_count: int
    model_source_catalogue: list | None = None


@dataclass(frozen=True)
class SourceRead:
    source: SourceDocument
    text: str = field(repr=False)
    start: int
    end: int
    total_code_points: int
    model_clues: list | None = None


class ValidationDataClient:
    def __init__(self, base_url, internal_api_key, context: ValidationContext, http_client=None):
        self.context = context
        self._base_url, self._key = base_url.rstrip("/"), internal_api_key
        self._http = http_client or httpx.AsyncClient(timeout=30)
        self._owns_http = http_client is None
        self.execution = context.job_id is not None
        self.model_settings = []
        self.author_correction_proofs = []

    async def _request(self, operation, **fields):
        context = self.context
        try:
            response = await self._http.post(
                f"{self._base_url}/api/internal/v1/works/{context.work_id}/validation-reports/{context.report_id}/data",
                headers={"X-Internal-Api-Key": self._key,
                         "X-Validation-Member-Id": str(context.member_id),
                         **({"X-Validation-Job-Id": str(context.job_id),
                             "X-Worker-Lease-Token": str(context.lease_token)} if self.execution else {})},
                json={"expectedBaselineHash": context.baseline_hash,
                      "expectedTargetHash": context.target_hash,
                      "operation": operation, "offset": 0, "limit": 100, **fields},
            )
            response.raise_for_status()
            envelope = response.json()
            if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
                raise IncompleteInspection("INVALID_DATA_CONTRACT")
            data = envelope["data"]
        except httpx.HTTPStatusError as exc:
            code = "INPUT_CHANGED" if exc.response.status_code == 409 else "DATA_ACCESS_FAILED"
            raise IncompleteInspection(code) from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise IncompleteInspection("DATA_READ_FAILED") from None
        if (type(data.get("formatVersion")) is not int or data.get("formatVersion") != 1
                or data.get("reportId") != str(context.report_id)
                or data.get("baselineHash") != context.baseline_hash
                or data.get("targetHash") != context.target_hash
                or type(data.get("baselineRevision")) is not int or data.get("baselineRevision") != context.baseline_revision):
            raise IncompleteInspection("INPUT_CHANGED")
        if self.execution and (type(data.get("projectionVersion")) is not int or data.get("projectionVersion") != 1
                or type(data.get("clueVersion")) is not int or data.get("clueVersion") != 1
                or data.get("manifestHash") != context.manifest_hash
                or context.report_revision is not None and data.get("reportRevision") != context.report_revision):
            raise IncompleteInspection("INPUT_CHANGED")
        if data.get("status") != "READY":
            status = data.get("status")
            raise IncompleteInspection(status if status in {"SOURCE_CHANGED", "SOURCE_PURGED",
                                       "SOURCE_UNREADABLE", "INVALID_RANGE"} else "DATA_NOT_READY")
        return data

    async def read_input(self):
        data = await self._request("INPUT")
        try:
            target = SourceDocument.from_wire(data["target"], target=True)
            sources = tuple(SourceDocument.from_wire(row) for row in data["sources"])
            if (target.content_hash != self.context.target_hash or type(data["entryCount"]) is not int
                    or data["entryCount"] < 0 or len({source.ref for source in sources}) != len(sources)
                    or any(source.kind == "EPISODE" and source.metadata["episodeNo"] >= target.metadata["episodeNo"]
                           for source in sources)):
                raise ValueError("Invalid frozen scope")
            catalogue = data.get("modelSourceCatalogue")
            if self.execution and (not isinstance(catalogue, list)
                    or any(not isinstance(row, dict) for row in catalogue)
                    or [row.get("source_ref") for row in catalogue] != [source.ref for source in sources]
                    or self.context.target_revision is not None
                    and target.metadata.get("contentRevision") != self.context.target_revision):
                raise ValueError("Missing trusted projection")
            return FrozenInput(self.context, target, sources, data["manifestHash"], data["entryCount"], catalogue)
        except (ValueError, KeyError, TypeError):
            raise IncompleteInspection("INVALID_INPUT_CONTRACT") from None

    async def read_entries_page(self, offset, limit):
        data = await self._request("ENTRIES", offset=offset, limit=limit)
        if (not isinstance(data.get("entries"), list) or type(data.get("nextOffset")) is not int
                or type(data.get("hasMore")) is not bool
                or data["nextOffset"] != offset + len(data["entries"])
                or data["hasMore"] and not data["entries"]):
            raise IncompleteInspection("INCOMPLETE_CATALOGUE")
        if self.execution:
            settings, proofs = data.get("modelSettings"), data.get("authorCorrectionProofs")
            if (not isinstance(settings, list) or not isinstance(proofs, list)
                    or any(not isinstance(row, dict) for row in settings)
                    or any(not isinstance(row, dict) for row in data["entries"])
                    or [row.get("entry_ref") for row in settings] != [row.get("entryRef") for row in data["entries"]]
                    or any(row.get("author_correction") is not None for row in settings)):
                raise IncompleteInspection("INVALID_BASELINE_CONTRACT")
            if offset == 0:
                self.model_settings, self.author_correction_proofs = [], []
            if offset != len(self.model_settings):
                raise IncompleteInspection("INCOMPLETE_CATALOGUE")
            self.model_settings.extend(settings)
            self.author_correction_proofs.extend(proofs)
        return data["entries"], data["nextOffset"], data["hasMore"]

    async def read_source(self, source: SourceDocument, start=None, end=None):
        data = await self._request(
            "READ_SOURCE", sourceRef=source.ref, expectedSourceHash=source.content_hash,
            expectedSourceVersion=source.source_version, start=start, end=end,
        )
        try:
            value = SourceRead(source, data["text"], data["start"], data["end"], data["totalCodePoints"])
            if (data["sourceRef"] != source.ref or data["contentHash"] != source.content_hash
                    or not isinstance(value.text, str)
                    or any(type(number) is not int for number in (value.start, value.end, value.total_code_points))
                    or not 0 <= value.start <= value.end <= value.total_code_points
                    or len(value.text) != value.end - value.start
                    or value.start != (0 if start is None else start)
                    or value.end != (value.total_code_points if end is None else end)
                    or value.start == 0 and value.end == value.total_code_points
                    and sha256(value.text.encode()).hexdigest() != source.content_hash):
                raise ValueError("Invalid actual source")
            if self.execution and source.ref == "T1" and start is None and end is None:
                clues = data.get("modelClues")
                if not isinstance(clues, list):
                    raise ValueError("Missing trusted clues")
                from app.validation.contracts import Span, validate_span
                for clue in clues:
                    validate_span(Span.model_validate(clue["span"]), value.text)
                value = SourceRead(source, value.text, value.start, value.end, value.total_code_points, clues)
            return value
        except (ValueError, KeyError, TypeError):
            raise IncompleteInspection("INVALID_SOURCE_CONTRACT") from None

    async def validate(self):
        await self._request("VALIDATE")

    async def aclose(self):
        if self._owns_http:
            await self._http.aclose()
