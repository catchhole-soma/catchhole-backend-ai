"""Offline synthetic before/after benchmark. No database, source files or model calls.

Run from the repository root with PYTHONPATH=. and --baseline-ref <full SHA>.
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
from tests.test_book_character_context import DEFAULT_SCHEMA_HINTS, inputs


class Captured(Exception):
    pass


class NoProvider:
    async def create_text_response(self, **kwargs):
        raise AssertionError('Benchmark must never call a provider')


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
    }
    return bodies[kind], values


async def measure(base, body, values):
    extractor = capture_type(base)(llm_client=NoProvider(), max_attempts=1)
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
    guarded = ensure_ordered_prompt_fits(system, user, 10**12, response_schema=response)
    selected = len(ordered.known_characters)
    if 'book_character_context:\n' in user:
        context = json.JSONDecoder().raw_decode(user.split('book_character_context:\n', 1)[1])[0]
        selected = len(context['characters'])
    return {'raw_tokens': raw, 'guard_tokens': guarded, 'selected_characters': selected,
            'within_limit': guarded <= 64000, 'prompt_sha256': hashlib.sha256((system+user).encode()).hexdigest()}


def baseline_class(ref):
    sha = subprocess.check_output(['git', 'rev-parse', ref], text=True).strip()
    source = subprocess.check_output(['git', 'show', f'{sha}:app/analysis/setting_extractor.py'], text=True)
    module = types.ModuleType('app.analysis._benchmark_baseline')
    module.__file__ = str(Path('app/analysis/setting_extractor.py').resolve())
    sys.modules[module.__name__] = module
    # Load only the explicitly chosen local Git revision, never manuscript text.
    exec(compile(source, module.__file__, 'exec'), module.__dict__)  # noqa: S102
    return sha, module.CharacterSettingExtractor


async def main(sha, baseline):
    rows = []
    for kind,count in [('one',400),('alias',400),('same_name',400),('twelve',133),('global',134),('numeric_era',134)]:
        body, values = fixture(kind,count)
        before = await measure(baseline, body, values)
        after = await measure(CharacterSettingExtractor, body, values)
        rows.append({'case':kind, 'characters':count, 'before':before, 'after':after,
                     'guard_saved':before['guard_tokens']-after['guard_tokens']})
    return {'format_version':1, 'measurement':'offline synthetic provider prompts, not model quality',
            'baseline_sha':sha, 'tokenizer':f'tiktoken {tiktoken.__version__} o200k_base',
            'code_sha256':{name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in
                           ('app/analysis/book_character_context.py','app/analysis/setting_extractor.py')},
            'rows':rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main(*baseline_class(args.baseline_ref))), ensure_ascii=False, indent=2))
