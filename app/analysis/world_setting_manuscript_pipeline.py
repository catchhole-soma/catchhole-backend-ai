"""Collect a bounded, ordered window using the existing Spring batch APIs."""

from collections import deque

from app.analysis.exceptions import OrderedInputContextError
from app.analysis.ordered_context import validate_comparison_source
from app.analysis.world_setting_batch_recovery import can_recover_response
from app.analysis.world_setting_manuscript import (
    WorldSettingManuscriptGroup, WorldSettingManuscriptOutcome,
)


class WorldSettingComparisonWindow:
    def __init__(self, pipeline, job_id, lease_token, analysis_context, analysis_source):
        self.pipeline = pipeline
        self.comparator = pipeline.manuscript_comparator
        self.job_id, self.lease_token = job_id, lease_token
        self.analysis_context, self.analysis_source = analysis_context, analysis_source
        self.pending = deque()
        self.cached = {}
        self.exhausted = False
        self.claim_error = None
        self.sequence = 0

    async def _claim(self):
        if self.exhausted:
            return None
        batch = await self.pipeline.spring_client.claim_next_world_setting_comparison_batch(
            self.job_id, self.lease_token,
        )
        self.exhausted = batch is None
        return batch

    async def claim_next(self):
        if self.pending:
            return self.pending.popleft()
        if self.claim_error is not None:
            raise self.claim_error
        return await self._claim()

    async def after_subject_resolution_reset(self):
        # Reset/re-resolution can create new pending work after lookahead reached
        # the end. Reclaim its earliest batch before saving any prefetched sibling;
        # those decisions must be regenerated against the new subject resolution.
        self.exhausted = False
        self.claim_error = None
        self.cached.clear()
        replacement = await self._claim()
        if replacement is not None:
            self.pending.appendleft(replacement)

    def _group(self, batch, context):
        if batch.analysis_context != self.analysis_context:
            raise OrderedInputContextError("World batch input state changed.")
        validate_comparison_source(batch.source, self.analysis_context, self.analysis_source)
        self.sequence += 1
        targets = {target.identity_key(): target for target in context.targets}
        keys = [f"world:{target}" for target in batch.resolved_target_world_setting_ids]
        keys.extend(batch.resolved_provisional_subject_keys)
        return WorldSettingManuscriptGroup(
            f"G{self.sequence}", batch.category, batch.candidates, [targets[key] for key in keys],
            ordered_context=batch.analysis_context is not None,
            unresolved_references=(tuple(context.analysis_context.unresolved_references)
                                   if context.analysis_context else ()),
            analysis_source=batch.source,
        )

    async def outcome(self, batch, context):
        """Use a cached decision only with the exact context sent to the model."""
        cached = self.cached.pop(batch.comparison_batch_id, None)
        if cached is not None:
            previous_context, outcome = cached
            # Includes candidate data, paths, provenance, versions and contextToken.
            # A new token cannot authorize a decision made against an old context.
            return outcome if previous_context == context else None
        first = self._group(batch, context)
        groups, batches, contexts = [first], [batch], [context]
        identities = first.target_keys() | {batch.canonical_subject_key}
        while True:
            if self.pending:
                following = self.pending.popleft()
            else:
                try:
                    following = await self._claim()
                except Exception as error:
                    # Preserve the already-claimed prefix; report the claim error
                    # at the same position as the old sequential loop.
                    self.claim_error = error
                    break
            if following is None:
                break
            following_keys = {f"world:{target}" for target in following.resolved_target_world_setting_ids}
            following_keys.update(following.resolved_provisional_subject_keys)
            following_keys.add(following.canonical_subject_key)
            if (identities & following_keys or following.work_id != batch.work_id
                    or following.source_episode_id != batch.source_episode_id
                    or following.source != batch.source):
                self.pending.appendleft(following)
                break
            try:
                following_context = await self.pipeline._load_batch_context(
                    self.job_id, self.lease_token, following,
                )
                group = self._group(following, following_context)
                if not self.comparator.fits([*groups, group]):
                    self.pending.appendleft(following)
                    break
            except Exception:
                # Its own normal pipeline iteration owns failure/stale handling.
                self.pending.appendleft(following)
                break
            groups.append(group)
            batches.append(following)
            contexts.append(following_context)
            identities.update(following_keys)
        # All already-claimed groups must still pass through normal completion,
        # error, quota and manual-stop handling in their original order.
        self.pending.extendleft(reversed(batches[1:]))
        if len(groups) == 1:
            return None
        try:
            outcomes = await self.comparator.compare_manuscript(groups)
        except Exception as error:
            if not can_recover_response(error):
                raise
            outcomes = {group.group_ref: WorldSettingManuscriptOutcome(error=error) for group in groups}
        for saved_batch, saved_context, group in zip(batches, contexts, groups, strict=True):
            self.cached[saved_batch.comparison_batch_id] = (saved_context, outcomes[group.group_ref])
        return self.cached.pop(batch.comparison_batch_id)[1]
