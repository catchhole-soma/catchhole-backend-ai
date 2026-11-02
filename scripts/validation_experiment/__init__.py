"""Local SETTING/TYPO validation experiment harness (grid v4).

Runs the real planner, judge and typo code in non-execution mode (no Java, no S3) against a fixture built from a
read-only export and a disposable loopback PostgreSQL. Every output goes outside this repository; production
derived data, manuscript text and API keys are never written here. See `run.py --help`.
"""
