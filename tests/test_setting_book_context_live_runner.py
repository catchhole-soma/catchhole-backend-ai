"""Offline checks for the opt-in evaluator; never create provider clients."""
import asyncio

from scripts.evaluate_setting_book_context_live import counterfactual_class, usage_total


def test_unknown_usage_is_not_reported_as_zero():
    assert usage_total([{'input_tokens': 12}, {}], 'input_tokens') is None
    assert usage_total([{'input_tokens': 12}, {'input_tokens': 8}], 'input_tokens') == 20
    assert usage_total([], 'input_tokens') == 0


def test_counterfactual_only_changes_application_guard_flag():
    class Baseline:
        async def _extract_once(self, **kwargs):
            return kwargs
    original = {'system_prompt': 'original system', 'user_prompt': 'original input',
                'requires_ordered_bound': True, 'max_output_tokens': 32000}
    replayed = asyncio.run(counterfactual_class(Baseline)()._extract_once(**original))
    assert replayed == {**original, 'requires_ordered_bound': False}
    assert original['requires_ordered_bound'] is True
