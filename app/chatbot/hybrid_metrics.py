"""Low-cardinality search diagnostics, without questions, identities or source text."""

from prometheus_client import CollectorRegistry, Counter, Histogram


class HybridMetrics:
    def __init__(self, registry=None):
        self.registry = registry or CollectorRegistry()
        self.operations = Counter(
            "chat_retrieval_operations",
            "Search/tool operations",
            ["method"],
            registry=self.registry,
        )
        self.chunks = Counter(
            "chat_retrieval_chunks",
            "Returned/used evidence chunks",
            ["stage"],
            registry=self.registry,
        )
        self.rounds = Histogram(
            "chat_retrieval_rounds",
            "Search rounds per question",
            buckets=(0, 1, 2),
            registry=self.registry,
        )
        self.latency = Histogram(
            "chat_retrieval_seconds", "Search operation latency", ["method"], registry=self.registry
        )
        self.outcomes = Counter(
            "chat_retrieval_outcomes",
            "Bounded result/failure labels",
            ["outcome"],
            registry=self.registry,
        )
        self.model_calls = Counter(
            "chat_retrieval_model_calls",
            "Actual model attempts including schema retry",
            ["purpose"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "chat_retrieval_tokens", "Measured provider usage", ["kind"], registry=self.registry
        )
