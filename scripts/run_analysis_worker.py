import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import signal
from typing import Protocol

from app.core.config import Settings, get_settings
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import AnalysisJobWorker, WorkerRunResult
from app.worker.world_setting_comparison_worker import WorldSettingComparisonWorker


class WorkerSchedulerApi(Protocol):
    async def claim_next(self) -> WorkerAnalysisJobPayload | None: ...

    async def process_claimed(
        self,
        payload: WorkerAnalysisJobPayload,
    ) -> WorkerRunResult: ...

    async def aclose(self) -> None: ...


AsyncSleeper = Callable[[float], Awaitable[None]]


async def run_worker_loop(
    worker: WorkerSchedulerApi,
    idle_sleep_seconds: float,
    concurrency: int,
    shutdown_grace_seconds: float,
    max_iterations: int | None = None,
    stop_event: asyncio.Event | None = None,
    sleeper: AsyncSleeper = asyncio.sleep,
) -> list[WorkerRunResult]:
    """빈 슬롯을 먼저 확보한 뒤 정확히 한 Job만 claim하는 비동기 scheduler."""

    if concurrency < 1:
        raise ValueError("concurrency must be at least 1.")
    if idle_sleep_seconds <= 0:
        raise ValueError("idle_sleep_seconds must be greater than zero.")
    if shutdown_grace_seconds <= 0:
        raise ValueError("shutdown_grace_seconds must be greater than zero.")

    stopping = stop_event or asyncio.Event()
    slots = asyncio.Semaphore(concurrency)
    active_tasks: set[asyncio.Task[WorkerRunResult | None]] = set()
    # 무한 운영에서는 완료 결과를 누적하지 않아 장기 실행 메모리가 증가하지 않게 한다.
    results: list[WorkerRunResult] = []
    collect_results = max_iterations is not None
    claim_attempts = 0
    wait_before_next_poll = False

    while not stopping.is_set():
        reached_iteration_limit = max_iterations is not None and claim_attempts >= max_iterations
        if reached_iteration_limit:
            break

        wait_before_next_poll = False
        while len(active_tasks) < concurrency and not stopping.is_set():
            if max_iterations is not None and claim_attempts >= max_iterations:
                break

            # 이 acquire가 claim보다 반드시 먼저 실행된다. payload가 반환된 뒤에는
            # 같은 Task가 즉시 처리하며 프로세스 내부 대기열에 Job을 보관하지 않는다.
            await slots.acquire()
            if stopping.is_set():
                slots.release()
                break
            claim_attempts += 1
            try:
                payload = await worker.claim_next()
            except asyncio.CancelledError:
                slots.release()
                raise
            except Exception as exc:
                slots.release()
                _print_failure(exc, phase="claim")
                wait_before_next_poll = True
                break

            if payload is None:
                slots.release()
                wait_before_next_poll = True
                break

            task = asyncio.create_task(
                _process_claimed_job(
                    worker,
                    payload,
                    slots,
                    results if collect_results else None,
                ),
                name=f"analysis-job-{payload.analysis_job_id}",
            )
            active_tasks.add(task)

        if max_iterations is not None and claim_attempts >= max_iterations:
            break

        await _wait_until_scheduler_can_progress(
            active_tasks,
            stopping,
            idle_sleep_seconds if wait_before_next_poll else None,
            sleeper,
        )
        active_tasks = {task for task in active_tasks if not task.done()}

    await _drain_active_jobs(active_tasks, shutdown_grace_seconds)
    return results


async def _process_claimed_job(
    worker: WorkerSchedulerApi,
    payload: WorkerAnalysisJobPayload,
    slots: asyncio.Semaphore,
    results: list[WorkerRunResult] | None,
) -> WorkerRunResult | None:
    try:
        result = await worker.process_claimed(payload)
    except asyncio.CancelledError:
        # 배포 강제 종료는 terminal FAILED로 바꾸지 않는다. heartbeat를 멈추고
        # Spring의 lease 만료/checkpoint 재회수 경로가 이어서 처리하게 한다.
        raise
    except Exception as exc:
        # Worker가 자기 Job 실패를 Spring에 보고한 뒤에도 다른 Job Task는 유지한다.
        _print_failure(exc, analysis_job_id=payload.analysis_job_id, phase="process")
        return None
    finally:
        slots.release()

    _print_result(result)
    if results is not None:
        results.append(result)
    return result


async def _wait_until_scheduler_can_progress(
    active_tasks: set[asyncio.Task[WorkerRunResult | None]],
    stop_event: asyncio.Event,
    idle_sleep_seconds: float | None,
    sleeper: AsyncSleeper,
) -> None:
    if not active_tasks:
        if idle_sleep_seconds is None:
            return
        await _wait_for_stop_or_sleep(stop_event, idle_sleep_seconds, sleeper)
        return

    stop_task = asyncio.create_task(stop_event.wait(), name="worker-stop-wait")
    helper_tasks: set[asyncio.Task] = {stop_task}
    if idle_sleep_seconds is not None:
        helper_tasks.add(
            asyncio.create_task(
                sleeper(idle_sleep_seconds),
                name="worker-idle-sleep",
            )
        )

    await asyncio.wait(active_tasks | helper_tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in helper_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*helper_tasks, return_exceptions=True)


async def _wait_for_stop_or_sleep(
    stop_event: asyncio.Event,
    seconds: float,
    sleeper: AsyncSleeper,
) -> None:
    stop_task = asyncio.create_task(stop_event.wait(), name="worker-stop-wait")
    sleep_task = asyncio.create_task(sleeper(seconds), name="worker-idle-sleep")
    await asyncio.wait({stop_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in (stop_task, sleep_task):
        if not task.done():
            task.cancel()
    await asyncio.gather(stop_task, sleep_task, return_exceptions=True)


async def _drain_active_jobs(
    active_tasks: set[asyncio.Task[WorkerRunResult | None]],
    shutdown_grace_seconds: float,
) -> None:
    if not active_tasks:
        return

    _, pending = await asyncio.wait(active_tasks, timeout=shutdown_grace_seconds)
    if not pending:
        return

    print(
        f"[{_timestamp()}] worker_shutdown_grace_expired "
        f"pending_job_count={len(pending)}; cancelling for lease recovery",
        flush=True,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def main() -> None:
    settings = get_settings()
    args = _parse_args(settings)
    asyncio.run(_async_main(args, settings))


async def _async_main(args: argparse.Namespace, settings: Settings) -> None:
    worker = (
        WorldSettingComparisonWorker(
            subject_resolution_model_name=(args.subject_resolution_model_name or args.model_name),
            comparison_model_name=args.comparison_model_name or args.model_name,
        )
        if args.worker_kind == "world-comparison"
        else AnalysisJobWorker(
            extraction_model_name=args.extraction_model_name or args.model_name,
            subject_resolution_model_name=(args.subject_resolution_model_name or args.model_name),
            comparison_model_name=args.comparison_model_name or args.model_name,
            embedding_generation_enabled=settings.embedding_generation_enabled,
        )
    )

    try:
        if args.once:
            _print_result(await worker.run_once())
            return

        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        await run_worker_loop(
            worker=worker,
            idle_sleep_seconds=args.idle_sleep_seconds,
            concurrency=_resolve_worker_concurrency(args.worker_kind, args.concurrency),
            shutdown_grace_seconds=args.shutdown_grace_seconds,
            max_iterations=args.max_iterations,
            stop_event=stop_event,
        )
    finally:
        await worker.aclose()


def _parse_args(settings: Settings | None = None) -> argparse.Namespace:
    settings = settings or get_settings()
    parser = argparse.ArgumentParser(description="Run CatchHole analysis worker.")
    parser.add_argument("--once", action="store_true", help="Run one claim attempt and exit.")
    parser.add_argument(
        "--worker-kind",
        choices=("analysis", "world-comparison"),
        default="analysis",
        help="Select the disjoint Spring job type set claimed by this process.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=settings.ai_worker_concurrency,
        help="Maximum concurrently running Jobs in this process.",
    )
    parser.add_argument(
        "--idle-sleep-seconds",
        type=float,
        default=settings.ai_worker_idle_sleep_seconds,
        help="Polling delay after Spring returns no claimable Job.",
    )
    parser.add_argument(
        "--shutdown-grace-seconds",
        type=float,
        default=settings.ai_worker_shutdown_grace_seconds,
        help="Time allowed for claimed Jobs to finish after SIGTERM/SIGINT.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Limit claim attempts for local checks. Omit for continuous worker mode.",
    )
    parser.add_argument(
        "--model-name",
        default=None,
        help="Deprecated common override for all LLM stages.",
    )
    parser.add_argument(
        "--extraction-model-name",
        default=None,
        help="Override the first-stage extraction model.",
    )
    parser.add_argument(
        "--subject-resolution-model-name",
        default=None,
        help="Override the character/world-setting subject resolution model.",
    )
    parser.add_argument(
        "--comparison-model-name",
        default=None,
        help="Override the second-stage comparison model.",
    )
    return parser.parse_args()


def _resolve_worker_concurrency(worker_kind: str, configured_concurrency: int) -> int:
    if worker_kind == "world-comparison":
        return 1
    return configured_concurrency


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        if not stop_event.is_set():
            print(f"[{_timestamp()}] worker_shutdown_requested", flush=True)
            stop_event.set()

    for signal_number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_number, request_stop)
        except NotImplementedError:
            signal.signal(signal_number, lambda *_: loop.call_soon_threadsafe(request_stop))


def _print_result(result: WorkerRunResult) -> None:
    print(
        f"[{_timestamp()}] "
        f"claimed={result.claimed} "
        f"analysis_job_id={result.analysis_job_id} "
        f"work_id={result.work_id} "
        f"work_title={result.work_title} "
        f"episode_count={result.episode_count} "
        f"message={result.message}",
        flush=True,
    )


def _print_failure(
    exc: BaseException,
    analysis_job_id=None,
    phase: str = "iteration",
) -> None:
    message = str(exc) or exc.__class__.__name__
    print(
        f"[{_timestamp()}] worker_{phase}_failed "
        f"analysis_job_id={analysis_job_id} error={message[:1000]}",
        flush=True,
    )


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
