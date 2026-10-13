"""Readiness, read-only planning and resumable preparation/generation."""

import asyncio
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import time

from sqlalchemy.exc import SQLAlchemyError

from app.chatbot.client import ChatTransportError
from app.retrieval.chunking import RetrievalSplitter
from app.retrieval.contracts import Limits, SourceChanged
from app.retrieval.provider import IndexProviderFailure


class RetrievalIndexService:
    def __init__(self, store, storage, spec, *, splitter=None):
        self.store, self.storage, self.spec = store, storage, spec
        self.splitter = splitter or RetrievalSplitter()

    def _slices(self, source):
        self.store.check_source(source)
        value = self.storage.get_text(source.key, version_id=source.version, max_bytes=2000000)
        if len(value) > 250000 or sha256(value.encode("utf-8")).hexdigest() != source.content_hash:
            raise SourceChanged()
        self.store.check_source(source)
        return self.splitter.split(value)

    def readiness(self, scope):
        sources, more = self.store.sources(scope)
        rows = [
            {
                "episodeId": str(s.episode_id),
                "episodeNo": s.episode_no,
                **self.store.inspect(s, self.spec),
            }
            for s in sources
        ]
        return {
            "schemaVersion": 1,
            "workId": str(scope.work_id),
            "spec": asdict(self.spec),
            "sources": rows,
            "hasMoreSources": more,
            "nextEpisodeFrom": sources[-1].episode_no + 1 if more else None,
            "supportedScopeReady": bool(rows)
            and not more
            and all(r["state"] == "READY" for r in rows),
            "supportedSourceTypes": ["CURRENT_EPISODE"],
            "unsupportedSourceTypes": ["SETTING_BOOK", "PAST_EPISODE_VERSION"],
            "excludedSources": self.store.excluded_sources(scope),
        }

    def dry_run(self, scope, limits=Limits()):
        sources, more = self.store.sources(scope)
        rows, tokens, targets, unknown, selected = [], 0, 0, 0, 0
        for source in sources:
            row = {
                "episodeId": str(source.episode_id),
                "episodeNo": source.episode_no,
                "state": source.state,
                "targetChunks": None,
                "inputTokens": None,
            }
            if source.state == "AVAILABLE":
                try:
                    slices = self._slices(source)
                    old = self.store.existing(source, self.spec)
                    ready = {
                        (c.chunk_index, c.start_offset, c.end_offset, c.chunk_hash)
                        for c in old
                        if self.store.chunk_state(c, self.spec) == "READY"
                    }
                    missing = [
                        c for c in slices if (c.index, c.start, c.end, c.content_hash) not in ready
                    ]
                    n = sum(c.tokens for c in missing)
                    row.update(
                        state="READY" if not missing else "NEEDS_INDEXING",
                        targetChunks=len(slices),
                        missingChunks=len(missing),
                        inputTokens=n,
                    )
                    tokens += n
                    targets += len(missing)
                    for c in missing:
                        if selected >= limits.max_chunks:
                            break
                        selected += 1
                except SourceChanged:
                    row["state"] = "SOURCE_CHANGED"
                    unknown += 1
                except SQLAlchemyError:
                    raise
                except Exception:
                    row["state"] = "SOURCE_UNREADABLE"
                    unknown += 1
            else:
                unknown += 1
            rows.append(row)
        cost = (
            None
            if limits.usd_per_million is None or unknown
            else str(Decimal(tokens) * limits.usd_per_million / Decimal(1000000))
        )
        return {
            "schemaVersion": 1,
            "mode": "DRY_RUN",
            "workId": str(scope.work_id),
            "spec": asdict(self.spec),
            "sources": rows,
            "hasMoreSources": more,
            "nextEpisodeFrom": sources[-1].episode_no + 1 if more else None,
            "targetMissingChunks": targets,
            "knownInputTokens": tokens,
            "unknownSourceCount": unknown,
            "estimatedCostUsd": cost,
            "priceUsdPerMillion": None
            if limits.usd_per_million is None
            else str(limits.usd_per_million),
            "maxChunks": limits.max_chunks,
            "maxInputTokensIncludingRetries": limits.max_input_tokens,
            "maxRetries": limits.max_retries,
            "concurrency": 1,
            "limited": more or targets > limits.max_chunks or tokens > limits.max_input_tokens,
            "userTokenDebit": False,
        }

    def prepare(self, scope):
        """Optional new-analysis producer. No provider, usage or analysis mutation."""
        sources, more = self.store.sources(scope)
        prepared = 0
        for source in sources:
            if source.state == "AVAILABLE":
                self.store.prepare(source, self.spec, self._slices(source))
                prepared += 1
        return {"preparedSources": prepared, "hasMoreSources": more}

    async def run(self, scope, provider, *, enabled: bool, limits=Limits()):
        if not enabled:
            return {"state": "DISABLED", "providerAttempts": 0, "userTokenDebit": False}
        report = {
            "state": "COMPLETED",
            "embeddedChunks": 0,
            "failedChunks": 0,
            "changedChunks": 0,
            "skippedReadyChunks": 0,
            "attemptedInputTokens": 0,
            "sourceFailures": [],
            "userTokenDebit": False,
        }
        started, selected = time.monotonic(), 0
        sources, more = await asyncio.to_thread(self.store.sources, scope)
        report["hasMoreSources"] = more
        report["nextEpisodeFrom"] = sources[-1].episode_no + 1 if more else None
        for source in sources:
            if selected >= limits.max_chunks:
                report["state"] = "CHUNK_LIMIT"
                break
            if time.monotonic() - started >= limits.max_seconds:
                report["state"] = "TIME_LIMIT"
                break
            if source.state != "AVAILABLE":
                report["sourceFailures"].append(
                    {"episodeId": str(source.episode_id), "code": source.state}
                )
                continue
            try:
                slices = await asyncio.to_thread(self._slices, source)
                chunks = await asyncio.to_thread(self.store.prepare, source, self.spec, slices)
            except SourceChanged:
                report["sourceFailures"].append(
                    {"episodeId": str(source.episode_id), "code": "SOURCE_CHANGED"}
                )
                continue
            except SQLAlchemyError:
                report["state"] = "DATABASE_FAILURE"
                break
            except Exception:
                report["sourceFailures"].append(
                    {"episodeId": str(source.episode_id), "code": "SOURCE_UNREADABLE"}
                )
                continue
            missing = [c for c in chunks if self.store.chunk_state(c, self.spec) != "READY"]
            report["skippedReadyChunks"] += len(chunks) - len(missing)
            for offset in range(0, len(missing), limits.batch_size):
                batch = missing[
                    offset : offset + min(limits.batch_size, limits.max_chunks - selected)
                ]
                if not batch:
                    report["state"] = "CHUNK_LIMIT"
                    break
                selected += len(batch)
                tokens = sum(c.token_count for c in batch)
                for attempt in range(limits.max_retries + 1):
                    total = report["attemptedInputTokens"] + tokens
                    if total > limits.max_input_tokens or (
                        limits.max_cost_usd is not None
                        and Decimal(total) * limits.usd_per_million / Decimal(1000000)
                        > limits.max_cost_usd
                    ):
                        report["state"] = "BUDGET_EXHAUSTED"
                        break
                    if time.monotonic() - started >= limits.max_seconds:
                        report["state"] = "TIME_LIMIT"
                        break
                    try:
                        await asyncio.to_thread(self.store.validate, source, self.spec, batch)

                        async def before_call(operation_id):
                            await asyncio.to_thread(
                                self.store.attempted, source, self.spec, batch, operation_id
                            )

                        # Unknown outcomes still consume this invocation's admission budget.
                        report["attemptedInputTokens"] = total
                        async with asyncio.timeout(
                            max(0.001, limits.max_seconds - (time.monotonic() - started))
                        ):
                            vectors = await provider.embed(source, batch, before_call)
                        await asyncio.to_thread(self.store.save, source, self.spec, batch, vectors)
                        report["embeddedChunks"] += len(batch)
                        break
                    except SourceChanged:
                        report["changedChunks"] += len(batch)
                        break
                    except IndexProviderFailure as exc:
                        try:
                            await asyncio.to_thread(
                                self.store.failed, source, self.spec, batch, exc.code
                            )
                        except SourceChanged:
                            report["changedChunks"] += len(batch)
                            break
                        except SQLAlchemyError:
                            report["state"] = "DATABASE_FAILURE"
                            break
                        if exc.retryable and attempt < limits.max_retries:
                            await asyncio.sleep(2**attempt)
                            continue
                        report["failedChunks"] += len(batch)
                        if not exc.retryable:
                            report["state"] = exc.code
                        break
                    except ChatTransportError:
                        report["state"] = "LEDGER_FAILURE"
                        break
                    except SQLAlchemyError:
                        report["state"] = "DATABASE_FAILURE"
                        break
                    except TimeoutError:
                        report["state"] = "PROVIDER_TIMEOUT"
                        break
                if (
                    report["state"] == "COMPLETED"
                    and offset + len(batch) < len(missing)
                    and selected >= limits.max_chunks
                ):
                    report["state"] = "CHUNK_LIMIT"
                if report["state"] != "COMPLETED":
                    break
            if report["state"] != "COMPLETED":
                break
        report.update(
            providerAttempts=provider.attempts,
            measuredInputTokens=provider.input_tokens,
            unknownUsageCalls=provider.unknown_usage,
        )
        if report["state"] == "COMPLETED" and (
            report["sourceFailures"] or report["failedChunks"] or report["changedChunks"] or more
        ):
            report["state"] = "PARTIAL"
        return report
