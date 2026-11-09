"""BOOK-only provider projection. The frozen claim and downstream state stay complete."""

import json
import re
from copy import deepcopy

from app.analysis.character_name_resolver import normalize_character_name
from app.analysis.exceptions import OrderedInputContextError

BOOK_CHARACTER_CONTEXT_INSTRUCTIONS = """
book_character_context.catalog는 전체 인물의 이름·별칭과 확정 여부를 ref로 구분한 식별 힌트입니다.
characters에는 이번 원문에 관련될 수 있는 대상의 CURRENT 상세만 있습니다. catalog에만 있는
인물의 현재값은 생략된 것이며 빈 상태·신규 인물·설정 부재를 뜻하지 않습니다.
같은 이름이나 이름 일부에 여러 ref가 걸려도 같은 인물로 합치지 말고 원문으로 구분하세요.
current_slots의 active_status는 그 슬롯에 대응하는 기존 활성 STATUS의 출처·보호·시점 정보입니다.
active_statuses에는 같은 현재 슬롯으로 합칠 수 없는 STATUS가 남아 있으며 둘 다 읽으세요.
임시 인물도 식별 문맥으로 사용하되 catalog나 CURRENT를 원문 근거로 인용하지 마세요.
""".strip()

# A name mention cannot scope a rule explicitly applying to a whole group.
_GENERAL_SCOPE = re.compile(
    r"전원|모든|전체|누구나|나머지|등장인물|인물들|캐릭터들|사람들|그들|이들|저들|"
    r"각자|각각|모두|일행|가문|가족|종족|구성원|\b(?:all|everyone|everybody|each)\b",
    re.IGNORECASE,
)
def select_book_character_refs(chunk_text, catalog):
    """Narrow only explicit `name: contents` rows; unscoped text keeps all details.

    A matching name somewhere in prose does not prove that other statements
    concern that owner. This intentionally gives up selection for narrative,
    group/unnamed headings and mixed documents rather than guessing their scope.
    """
    all_refs = {row['ref'] for row in catalog}
    if _GENERAL_SCOPE.search(chunk_text):
        return all_refs
    spellings = {}
    for row in catalog:
        names = {normalize_character_name(value) for value in (row['name'], *row['aliases']) if value}
        # Even a one-character short heading can have several owners. Keeping
        # every possible owner is selection, never an identity resolution.
        spellings[row['ref']] = names | {part for name in names for part in re.findall(r'\w+', name)}
    selected = set()
    for line in chunk_text.splitlines():
        line = normalize_character_name(line)
        if not line:
            continue
        match = re.fullmatch(r'(?:[-*]\s+)?([^:：]+)[:：]\s*(.+)', line)
        if match is None:
            return all_refs
        heading, contents = normalize_character_name(match[1]), match[2]
        owners = {ref for ref, names in spellings.items() if heading in names}
        if not owners:
            return all_refs
        selected.update(owners)
        # Explicitly referenced people in a named row also keep their details.
        # Broad right matching includes compound particles; false positives
        # merely keep extra owners. Era prefixes still cannot match a name.
        selected.update(ref for ref, names in spellings.items()
                        if any(re.search(r'(?<!\w)' + re.escape(name), contents) for name in names if name))
    return selected or all_refs


def _merge_current(row, current):
    row = deepcopy(row)
    quotes = list(row.pop('identity_evidence', ()))
    for quote in current['identity_evidence']:
        if quote not in quotes:
            quotes.append(quote)
    row['identity_evidence'] = quotes
    row['current_slots'] = deepcopy(current['current_slots'])
    remaining = []
    for status in row['active_statuses']:
        slot = next((slot for slot in row['current_slots']
                     if slot['fact_type'] == 'STATUS' and slot['fact_key'] == status['fact_key']
                     and slot['fact_value'] == status['fact_value']
                     and not (isinstance(slot['value_json'], dict) and slot['value_json'].get('active') is False)
                     and 'active_status' not in slot), None)
        if slot is None:
            remaining.append(status)
        else:
            slot['active_status'] = {key: value for key, value in status.items()
                                     if key not in ('fact_key', 'fact_value')}
    row['active_statuses'] = remaining
    return row


def book_character_prompt_data(chunk_text, ordered_context, semantic_current_context):
    ordered = ordered_context.prompt_data()
    current = json.loads(semantic_current_context.character_prompt_json())['characters']
    by_ref = {row['ref']: row for row in current}
    expected = {f'K{i}' for i in range(1, len(ordered['characters']) + 1)}
    if set(by_ref) != expected or len(by_ref) != len(current):
        raise OrderedInputContextError('book_character_context_identity_mismatch')
    catalog, details = [], []
    for index, row in enumerate(ordered['characters'], 1):
        ref = f'K{index}'
        value = by_ref[ref]
        if row['name'] != value['name'] or list(row['aliases']) != value['aliases']:
            raise OrderedInputContextError('book_character_context_identity_mismatch')
        merged = _merge_current(row, value)
        catalog.append({'ref': ref, **{key: merged.pop(key) for key in
                                     ('name', 'aliases', 'confirmation_status')}})
        details.append({'ref': ref, **merged})
    selected = select_book_character_refs(chunk_text, catalog)
    return {'catalog': catalog, 'characters': [row for row in details if row['ref'] in selected],
            'unresolved_references': ordered['unresolved_references']}


def book_character_prompt_json(chunk_text, ordered_context, semantic_current_context):
    return json.dumps(book_character_prompt_data(chunk_text, ordered_context, semantic_current_context),
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))
