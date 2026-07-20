import re
from uuid import UUID

from app.worker.analysis_job_worker import WorkerRunResult
from scripts.run_analysis_worker import _print_result, run_worker_loop

ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000001")


def test_print_result_prefixes_timestamp_when_job_does_not_exist(capsys) -> None:
    _print_result(
        WorkerRunResult(
            claimed=False,
            analysis_job_id=None,
            message="Claimable analysis job does not exist.",
        )
    )

    output = capsys.readouterr().out.strip()

    assert re.match(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\] ", output)
    assert "claimed=False" in output
    assert "message=Claimable analysis job does not exist." in output


def test_run_worker_loop_repeats_run_once_and_sleeps_when_job_does_not_exist() -> None:
    # 첫 번째 반복은 job을 claim한 상황, 두 번째 반복은 claim할 job이 없는 상황을 흉내낸다.
    worker = FakeWorker(
        results=[
            WorkerRunResult(
                claimed=True,
                analysis_job_id=ANALYSIS_JOB_ID,
                message="Analysis job completed.",
            ),
            WorkerRunResult(
                claimed=False,
                analysis_job_id=None,
                message="Claimable analysis job does not exist.",
            ),
        ]
    )
    # 실제 time.sleep을 쓰면 테스트가 느려지므로, 호출된 sleep 시간만 list에 기록한다.
    sleeps: list[float] = []

    # max_iterations=2로 무한 loop를 막고, sleeper를 주입해 sleep 호출 여부만 검증한다.
    results = run_worker_loop(
        worker=worker,
        idle_sleep_seconds=3.0,
        max_iterations=2,
        sleeper=sleeps.append,
    )

    # run_once가 지정한 반복 횟수만큼 호출됐는지 확인
    assert worker.run_count == 2
    # 각 반복에서 반환한 claimed 상태가 그대로 수집됐는지 확인
    assert [result.claimed for result in results] == [True, False]
    # claim할 job이 없던 두 번째 반복에서만 idle sleep이 호출됐는지 확인
    assert sleeps == [3.0]


class FakeWorker:
    # 실제 Worker 대신 run_once 결과만 순서대로 반환한다.
    def __init__(self, results: list[WorkerRunResult]) -> None:
        self.results = results
        self.run_count = 0

    def run_once(self) -> WorkerRunResult:
        # 미리 준비한 결과를 하나씩 반환해서 runner loop만 독립적으로 테스트한다.
        result = self.results[self.run_count]
        self.run_count += 1
        return result
