import argparse
import time
from collections.abc import Callable
from datetime import datetime

from app.core.config import get_settings
from app.worker.analysis_job_worker import AnalysisJobWorker, WorkerRunResult


# AnalysisJobWorker.run_once()를 반복 호출하는 CLI runner의 loop
def run_worker_loop(
    worker: AnalysisJobWorker,
    idle_sleep_seconds: float,
    max_iterations: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[WorkerRunResult]:
    # 실행 결과를 모아두면 테스트나 수동 점검에서 어떤 흐름으로 돌았는지 확인하기 쉽다.
    results: list[WorkerRunResult] = []
    iteration = 0

    # max_iterations가 없으면 계속 돌고, 테스트나 수동 확인에서는 횟수를 제한할 수 있다.
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        # run_once는 Spring claim부터 분석 완료/실패 보고까지 job 하나만 처리
        try:
            result = worker.run_once()
        except Exception as exc:
            # run_once가 해당 job의 실패 상태를 Spring에 보고한 뒤 예외를 다시 던져도
            # 장기 실행 Worker는 다음 회차 job을 계속 claim한다.
            _print_failure(exc)
            sleeper(idle_sleep_seconds)
            continue
        results.append(result)

        _print_result(result)

        # 가져갈 job이 없을 때만 잠깐 쉬었다가 다시 claim을 시도
        if not result.claimed:
            sleeper(idle_sleep_seconds)

    return results


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    # 실제 실행에서는 세부 서비스를 직접 넣지 않고 Worker가 기본 구현체를 필요할 때 준비
    worker = AnalysisJobWorker(
        model_name=args.model_name,
        embedding_generation_enabled=settings.embedding_generation_enabled,
    )

    # --once는 로컬에서 Spring claim 연결만 빠르게 확인할 때 사용한다.
    if args.once:
        _print_result(worker.run_once())
        return

    # 기본 실행은 Worker 프로세스처럼 계속 claim을 시도하는 모드
    run_worker_loop(
        worker=worker,
        idle_sleep_seconds=args.idle_sleep_seconds,
        max_iterations=args.max_iterations,
    )


def _parse_args() -> argparse.Namespace:
    # argparse는 Python 표준 CLI 인자 파서, 별도 라이브러리 없이 실행 옵션을 받음
    parser = argparse.ArgumentParser(description="Run CatchHole analysis worker.")
    # 한 번만 시행할지
    parser.add_argument("--once", action="store_true", help="Run one claim attempt and exit.")
    parser.add_argument(
        # sleep 시간, 기본 5초
        "--idle-sleep-seconds",
        type=float,
        default=5.0,
        help="Sleep seconds when claimable job does not exist.",
    )
    parser.add_argument(
        # 최대 반복 횟수, 기본 제한 없음
        "--max-iterations",
        type=int,
        default=None,
        help="Limit loop iterations for local checks. Omit for continuous worker mode.",
    )
    parser.add_argument(
        # 사용할 모델 이름
        "--model-name",
        default=None,
        help="Override LLM model name passed to Spring claim and extractor.",
    )
    return parser.parse_args()


def _print_result(result: WorkerRunResult) -> None:
    # 운영 로깅 전 단계의 단순 출력, 로컬에서 claim 여부와 job id를 바로 확인하기 위함
    print(
        f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
        f"claimed={result.claimed} "
        f"analysis_job_id={result.analysis_job_id} "
        f"work_id={result.work_id} "
        f"work_title={result.work_title} "
        f"episode_count={result.episode_count} "
        f"message={result.message}",
        flush=True,
    )


def _print_failure(exc: Exception) -> None:
    message = str(exc) or exc.__class__.__name__
    print(
        f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
        f"worker_iteration_failed error={message[:1000]}",
        flush=True,
    )


if __name__ == "__main__":
    main()
