#!/usr/bin/env python3
"""Docker의 실제 host port mapping으로 운영 Prometheus file_sd JSON을 만든다."""

import argparse
import ipaddress
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

SERVICES = {
    "ai-worker": "analysis",
    "ai-character-comparison-worker": "character-comparison",
    "ai-world-comparison-worker": "world-comparison",
}


def build_targets(containers: list[dict], expected_analysis: int = 5) -> list[dict]:
    if expected_analysis < 1:
        raise ValueError("분석 Worker 예상 수는 1 이상이어야 합니다.")
    targets: list[dict] = []
    counts: Counter = Counter()
    seen_addresses: set[str] = set()
    seen_replicas: set[tuple[str, int]] = set()
    for item in containers:
        labels = item.get("Config", {}).get("Labels", {}) or {}
        service = labels.get("com.docker.compose.service")
        if labels.get("com.docker.compose.project") != "catchhole-worker" or service not in SERVICES:
            continue
        if not item.get("State", {}).get("Running"):
            continue
        replica = int(labels.get("com.docker.compose.container-number", "0"))
        if replica < 1 or (service, replica) in seen_replicas:
            raise ValueError("Worker replica 번호가 없거나 중복됩니다.")
        seen_replicas.add((service, replica))
        bindings = item.get("NetworkSettings", {}).get("Ports", {}).get("9102/tcp") or []
        if len(bindings) != 1:
            raise ValueError("Worker metrics host binding은 정확히 하나여야 합니다.")
        address = ipaddress.IPv4Address(bindings[0]["HostIp"])
        private_ranges = (ipaddress.IPv4Network("10.0.0.0/8"),
                          ipaddress.IPv4Network("172.16.0.0/12"),
                          ipaddress.IPv4Network("192.168.0.0/16"))
        if not any(address in network for network in private_ranges):
            raise ValueError("운영 수집은 Worker EC2의 사설 IPv4 bind가 필요합니다.")
        port = int(bindings[0]["HostPort"])
        if not 1 <= port <= 65535:
            raise ValueError("Worker metrics host port가 유효하지 않습니다.")
        target = f"{address}:{port}"
        if target in seen_addresses:
            raise ValueError("Worker 수집 주소가 중복됩니다.")
        seen_addresses.add(target)
        counts[service] += 1
        targets.append({
            "targets": [target],
            "labels": {"environment": "prod", "application": "catchhole-ai",
                       "worker_kind": SERVICES[service], "worker_replica": str(replica)},
        })
    expected = {"ai-worker": expected_analysis, "ai-character-comparison-worker": 1,
                "ai-world-comparison-worker": 1}
    if dict(counts) != expected:
        raise ValueError("실행 중인 Worker 수가 예상과 다릅니다. 기존 targets를 유지하세요.")
    return sorted(targets, key=lambda group: (group["labels"]["worker_kind"],
                                             int(group["labels"]["worker_replica"])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-json", type=Path, help="테스트·오프라인용 Docker inspect JSON")
    parser.add_argument("--expected-analysis", type=int, default=5)
    args = parser.parse_args()
    try:
        if args.inspect_json:
            containers = json.loads(args.inspect_json.read_text())
        else:
            ids = subprocess.check_output([
                "docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=catchhole-worker",
            ], text=True).split()
            containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True)) if ids else []
        groups = build_targets(containers, args.expected_analysis)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError):
        print("Worker targets 생성 실패: 프로세스 수와 사설 metrics port mapping을 확인하세요.", file=sys.stderr)
        return 1
    print(json.dumps(groups, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
