import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "generate_worker_targets.py"
spec = importlib.util.spec_from_file_location("worker_targets", MODULE_PATH)
worker_targets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker_targets)


def container(service, replica, port, address="10.0.1.23", running=True):
    return {
        "Config": {"Labels": {
            "com.docker.compose.project": "catchhole-worker",
            "com.docker.compose.service": service,
            "com.docker.compose.container-number": str(replica),
        }},
        "State": {"Running": running},
        "NetworkSettings": {"Ports": {"9102/tcp": [{"HostIp": address, "HostPort": str(port)}]}},
    }


def seven_workers():
    return [container("ai-worker", i + 1, 9102 + i) for i in range(5)] + [
        container("ai-character-comparison-worker", 1, 9107),
        container("ai-world-comparison-worker", 1, 9108),
    ]


class WorkerTargetsTest(unittest.TestCase):
    def test_uses_actual_mapping_and_covers_each_replica(self):
        containers = seven_workers()
        containers[0]["NetworkSettings"]["Ports"]["9102/tcp"][0]["HostPort"] = "9110"
        groups = worker_targets.build_targets(containers, expected_analysis=5)
        self.assertEqual(len(groups), 7)
        self.assertEqual(len({g["targets"][0] for g in groups}), 7)
        self.assertIn("10.0.1.23:9110", [g["targets"][0] for g in groups])
        self.assertEqual(sum(g["labels"]["worker_kind"] == "analysis" for g in groups), 5)
        self.assertEqual({g["labels"]["environment"] for g in groups}, {"prod"})

    def test_missing_worker_does_not_publish_smaller_target_list(self):
        with self.assertRaises(ValueError):
            worker_targets.build_targets(seven_workers()[:-1], expected_analysis=5)

    def test_stopped_worker_does_not_count_as_running(self):
        containers = seven_workers()
        containers[2]["State"]["Running"] = False
        with self.assertRaises(ValueError):
            worker_targets.build_targets(containers, expected_analysis=5)

    def test_rejects_public_loopback_and_wildcard_bindings(self):
        for address in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "::"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                worker_targets.build_targets(seven_workers() + [container("ai-worker", 6, 9111, address)], expected_analysis=6)

    def test_rejects_duplicate_targets_and_replica_numbers(self):
        for duplicate_port in (True, False):
            containers = seven_workers()
            if duplicate_port:
                containers[1]["NetworkSettings"]["Ports"]["9102/tcp"][0]["HostPort"] = "9102"
            else:
                containers[1]["Config"]["Labels"]["com.docker.compose.container-number"] = "1"
            with self.assertRaises(ValueError):
                worker_targets.build_targets(containers, expected_analysis=5)

    def test_ignores_unrelated_project_and_requires_all_bindings(self):
        extra = container("ai-worker", 6, 9111)
        extra["Config"]["Labels"]["com.docker.compose.project"] = "another-project"
        self.assertEqual(len(worker_targets.build_targets(seven_workers() + [extra], expected_analysis=5)), 7)
        containers = seven_workers()
        containers[0]["NetworkSettings"]["Ports"]["9102/tcp"] = None
        with self.assertRaises(ValueError):
            worker_targets.build_targets(containers, expected_analysis=5)


if __name__ == "__main__":
    unittest.main()
