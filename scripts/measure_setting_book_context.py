"""Offline synthetic before/after benchmark. No database, source files or model calls.

Run from the repository root with PYTHONPATH=. and --baseline-ref <full SHA>.
The modified extractor's stage-1 selection is answered by a deterministic stand-in
(catalogue names literally present in the chunk), so `after` guard numbers describe the
projection for that selection, not model behaviour. Both baseline modules are loaded from the
chosen Git revision so the comparison never imports the modified `book_character_context`.
"""
import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import types
from pathlib import Path
from uuid import UUID

import tiktoken

from app.analysis.ordered_context import ensure_ordered_prompt_fits
from app.analysis.setting_extractor import (
    SETTING_EXTRACTION_RESPONSE_SCHEMA,
    CharacterSettingExtractor,
)
from app.core.config import get_settings
from app.llm.responses import LlmTextResponse
from tests.book_selection_fakes import fake_book_selection
from tests.test_book_character_context import DEFAULT_SCHEMA_HINTS, inputs

BASELINE_MODULES = ('app/analysis/book_character_context.py', 'app/analysis/setting_extractor.py')


class Captured(Exception):
    pass


class NoProvider:
    """Answers only the offline selection stand-in; the extraction boundary is never crossed."""

    def __init__(self):
        self.selection_prompts = []

    async def create_text_response(self, **kwargs):
        selection = fake_book_selection(kwargs)
        if selection is None:
            raise AssertionError('Benchmark must never call a provider')
        self.selection_prompts.append((kwargs['system_prompt'], kwargs['user_prompt'], kwargs['response_schema']))
        return LlmTextResponse(text=json.dumps(selection, ensure_ascii=False))


def capture_type(base):
    class Capture(base):
        async def _extract_once(self, system_prompt, user_prompt, *args, **kwargs):
            self.prompts = system_prompt, user_prompt
            raise Captured
    return Capture


def fixture(kind, count):
    names = [f'합성인물{i:03}' for i in range(count)]
    if kind == 'same_name':
        names[1] = names[0]
    values = inputs(tuple(names), aliases={0: ['청람']})
    bodies = {
        'one': '합성인물000: level=12', 'alias': '청람: level=12',
        'same_name': '합성인물000: level=12',
        'twelve': '\n'.join(f'합성인물{i:03}: level=12' for i in range(12)),
        'global': '모든 인물의 성년은 20세다.',
        'numeric_era': '3합성인물000 2000년은 배경 시대다.',
        'narrative_two': '합성인물001은 합성인물002의 스승이며 왕국 북부 출신이다.',
    }
    return bodies[kind], values


def _guard(system, user, schema):
    return ensure_ordered_prompt_fits(system, user, 10**12, response_schema=schema)


async def measure(base, body, values):
    provider = NoProvider()
    extractor = capture_type(base)(llm_client=provider, max_attempts=1)
    ordered, semantic, book = values
    try:
        await extractor.extract_from_chunk(UUID(int=8), body, schema_hints=DEFAULT_SCHEMA_HINTS,
            ordered_context=ordered, semantic_current_context=semantic, setting_book=book)
    except Captured:
        pass
    else:
        raise AssertionError('Provider boundary was not reached')
    system, user = extractor.prompts
    response = SETTING_EXTRACTION_RESPONSE_SCHEMA
    encoding = tiktoken.get_encoding('o200k_base')
    raw = len(encoding.encode(system+user, disallowed_special=())) + len(encoding.encode(json.dumps({
        'type': 'json_schema', 'name': response.name, 'schema': response.schema, 'strict': response.strict,
    }, ensure_ascii=False, sort_keys=True), disallowed_special=()))
    guarded = _guard(system, user, response)
    selected = len(ordered.known_characters)
    if 'book_character_context:\n' in user:
        context = json.JSONDecoder().raw_decode(user.split('book_character_context:\n', 1)[1])[0]
        selected = len(context['characters'])
    selection_guard = sum(_guard(*prompt) for prompt in provider.selection_prompts)
    limit = get_settings().llm_setting_extraction_max_input_tokens
    return {'raw_tokens': raw, 'guard_tokens': guarded, 'selected_characters': selected,
            'selection_calls': len(provider.selection_prompts), 'selection_guard_tokens': selection_guard,
            'total_guard_tokens': guarded + selection_guard,
            'within_previous_limit_64000': guarded <= 64000 and all(_guard(*p) <= 64000 for p in provider.selection_prompts),
            'within_limit': guarded <= limit and all(_guard(*p) <= limit for p in provider.selection_prompts),
            'prompt_sha256': hashlib.sha256((system+user).encode()).hexdigest()}


def baseline_class(ref):
    """Load the baseline extractor AND its book context module from `ref`, isolated from HEAD."""
    sha = subprocess.check_output(['git', 'rev-parse', ref], text=True).strip()
    context_name = 'app.analysis._benchmark_baseline_book_context'
    context_source = subprocess.check_output(['git', 'show', f'{sha}:{BASELINE_MODULES[0]}'], text=True)
    context_module = types.ModuleType(context_name)
    context_module.__file__ = str(Path(BASELINE_MODULES[0]).resolve())
    sys.modules[context_name] = context_module
    exec(compile(context_source, context_module.__file__, 'exec'), context_module.__dict__)  # noqa: S102
    source = subprocess.check_output(['git', 'show', f'{sha}:{BASELINE_MODULES[1]}'], text=True)
    source = source.replace('from app.analysis.book_character_context import', f'from {context_name} import')
    if 'from app.analysis.book_character_selection import' in source:
        raise ValueError('Baseline already contains the selection stage; choose an older --baseline-ref')
    module = types.ModuleType('app.analysis._benchmark_baseline')
    module.__file__ = str(Path(BASELINE_MODULES[1]).resolve())
    sys.modules[module.__name__] = module
    # Load only the explicitly chosen local Git revision, never manuscript text.
    exec(compile(source, module.__file__, 'exec'), module.__dict__)  # noqa: S102
    return sha, module.CharacterSettingExtractor


async def main(sha, baseline):
    rows = []
    for kind,count in [('one',400),('alias',400),('same_name',400),('twelve',133),('global',134),
                       ('numeric_era',134),('narrative_two',134)]:
        body, values = fixture(kind,count)
        before = await measure(baseline, body, values)
        after = await measure(CharacterSettingExtractor, body, values)
        rows.append({'case':kind, 'characters':count, 'before':before, 'after':after,
                     'guard_saved':before['guard_tokens']-after['guard_tokens'],
                     'total_guard_saved':before['total_guard_tokens']-after['total_guard_tokens']})
    return {'format_version':3, 'input_limit': get_settings().llm_setting_extraction_max_input_tokens, 'measurement':'offline synthetic provider prompts with a deterministic selection stand-in, not model quality',
            'baseline_sha':sha, 'tokenizer':f'tiktoken {tiktoken.__version__} o200k_base',
            'code_sha256':{name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in
                           (*BASELINE_MODULES, 'app/analysis/book_character_selection.py')},
            'rows':rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main(*baseline_class(args.baseline_ref))), ensure_ascii=False, indent=2))
