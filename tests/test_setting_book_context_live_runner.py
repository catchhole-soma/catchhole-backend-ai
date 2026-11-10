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


def test_call_stage_and_selection_parsing_are_count_only():
    from scripts.evaluate_setting_book_context_live import (
        EVALUATED_FILES,
        call_stage,
        parse_selection,
    )
    assert call_stage('book-character-selection:v1') == 'selection'
    assert call_stage('setting-extraction:v12:abc') == 'extraction'
    parsed = parse_selection('{"selected":[{"ref":"K2","mention":"그녀","basis":"PRONOUN"}],'
                             '"ambiguous":[{"mention":"아린","candidate_refs":["K1","K2"]}],"unmatched_mentions":["하린"]}')
    assert parsed == {'selected_refs': ['K2'], 'ambiguous_refs': ['K1'], 'unmatched_mentions': ['하린']}
    assert parse_selection('not json') is None
    assert 'app/analysis/book_character_selection.py' in EVALUATED_FILES


def test_baseline_loader_isolates_both_book_modules():
    import subprocess
    import sys

    import pytest

    from scripts.measure_setting_book_context import baseline_class
    main_before_selection = 'b4bbce1217e4ad22546e0c0fefa6eb9413b3e21b'  # main after #148, before the selection stage
    try:
        subprocess.check_output(['git', 'cat-file', '-e', f'{main_before_selection}^{{commit}}'], stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        pytest.skip('baseline commit not available in this checkout')
    sha, extractor = baseline_class(main_before_selection)
    assert sha == main_before_selection
    module = sys.modules[extractor.__module__]
    assert module.book_character_prompt_json.__module__ == 'app.analysis._benchmark_baseline_book_context'
    assert 'select_book_characters' not in module.__dict__
