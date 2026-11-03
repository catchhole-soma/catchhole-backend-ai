"""Job-less data client over an experiment fixture: the same projection the server returns (projection 2).

Sources R1..Rn are the prior episodes in the server's manifest order (episode number descending), carrying the
exported episode ids, hashes and storage keys. Only the target is ever read as text; the grid v4 Worker reads
prior episodes through the retrieval index, never through READ_SOURCE.
"""

import json
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from app.validation.contracts import IncompleteInspection
from app.validation.data import FrozenInput, SourceDocument, SourceRead, ValidationContext
from app.validation.projection import current_settings, model_settings, subject_catalogue
from scripts.validation_experiment.common import HarnessError, manuscript_path, read_manuscript


class FixtureDataClient:
    execution = False

    def __init__(self, fixture, manuscripts):
        target_path = manuscript_path(manuscripts, fixture["target"]["episodeNo"], fixture["target"]["file"])
        self.target_text = read_manuscript(target_path)
        target_hash = sha256(self.target_text.encode()).hexdigest()
        if target_hash != fixture["target"]["contentHash"]:
            raise HarnessError("Target manuscript changed since the fixture was built")
        baseline = sha256(json.dumps(fixture["baselineEntries"], ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
        work = UUID(fixture["work"]["workId"])
        self.context = ValidationContext(fixture["work"]["memberId"], work, uuid5(NAMESPACE_URL, f"experiment:{baseline}"),
                                         baseline, target_hash)
        self.target = SourceDocument("T1", "EPISODE", target_hash, None, {
            "episodeId": str(uuid5(work, "target")), "episodeNo": fixture["target"]["episodeNo"]})
        prior = sorted(fixture["episodes"], key=lambda row: row["episodeNo"], reverse=True)
        self.sources = tuple(SourceDocument(f"R{index}", "EPISODE", row["contentHash"], row.get("contentS3Version"), {
            "sourceKind": "EPISODE", "sourceRef": f"R{index}", "episodeId": row["episodeId"],
            "episodeNo": row["episodeNo"], "contentHash": row["contentHash"], "contentS3Key": row["contentS3Key"],
            "contentS3Version": row.get("contentS3Version")}) for index, row in enumerate(prior, 1))
        entries = [{"entryRef": f"B{index}", **row} for index, row in enumerate(fixture["baselineEntries"], 1)]
        self.rows = model_settings(entries, self.sources)
        self.entry_count = len(entries)
        self.manifest_hash = sha256(json.dumps([source.metadata for source in self.sources], sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
        self.operations = []

    async def read_input(self):
        self.operations.append("INPUT")
        return FrozenInput(self.context, self.target, self.sources, self.manifest_hash, self.entry_count)

    async def read_source(self, source, start=None, end=None):
        self.operations.append("READ_SOURCE")
        if source.ref != "T1":
            raise IncompleteInspection("SOURCE_UNREADABLE")
        text = self.target_text
        start = 0 if start is None else start
        end = len(text) if end is None else end
        return SourceRead(source, text[start:end], start, end, len(text))

    async def read_subjects(self):
        self.operations.append("SUBJECTS")
        return subject_catalogue(self.rows)

    async def read_settings(self, subject_refs):
        self.operations.append("SETTINGS")
        return current_settings(self.rows, subject_refs)

    async def validate(self):
        self.operations.append("VALIDATE")
