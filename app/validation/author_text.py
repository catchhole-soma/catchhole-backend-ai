"""Author-facing decision text without internal refs or pipeline words (owner feedback on the 31화 report, 2026-11-02).

The judge prompt already asks for plain sentences; this is the Worker's deterministic safety net for the strings an
author reads (`reason`, `impact_reason`, `disagreements[].reason`). Completion reasons are audit records and keep
their refs. Known refs become author words ("이번 원고", "18화", "설정집", "저장된 설정", a subject name) with the
following Korean particle fixed; a sentence that still holds an unknown ref or an internal word is dropped, and an
empty result falls back to a fixed sentence. Quotes, names and setting values are never rewritten otherwise.
"""

import re

TARGET_LABEL = "이번 원고"
SETTING_LABEL = "저장된 설정"
BOOK_LABEL = "설정집"
SUBJECT_FALLBACK = "대상"

REASON_FALLBACKS = {
    "CONFLICT": "저장된 설정과 이번 원고의 내용이 서로 맞지 않아요.",
    "SUSPECTED_CONFLICT": "저장된 설정과 달라 보이지만 그 사이의 변화나 예외를 찾지 못했어요.",
    "NEEDS_CONTEXT": "대상·시점·해석이 분명하지 않아 비교 판단이 어려워요.",
    "NO_CONFLICT": "저장된 설정과 이번 원고가 서로 맞아요.",
}
IMPACT_REASON_FALLBACKS = {
    "SEVERE": "장면의 행동이나 사건 성립에 직접 영향을 줘요.",
    "CAUTION": "배경이나 관계 이해에 영향을 줄 수 있어요.",
    "REFERENCE": "부수 묘사라 영향이 작아요.",
}
DISAGREEMENT_FALLBACK = "저장된 설정과 이전 원고의 내용이 서로 달라요."
COUNTER = "AUTHOR_TEXT_SANITIZED"

# Next to Hangul `\b` does not split ("T1의"), so ASCII-only lookarounds decide the token edges.
_TOKEN = re.compile(r"(?<![A-Za-z0-9])(T1|TSEG[1-9]\d*|[EBRSLQ][1-9]\d*)(?![0-9])")
JARGON_WORDS = ("limited", "receipt", "receipts", "readiness", "vector", "baseline", "target_context", "keyword",
                "evidence_refs", "receipt_refs", "source_ref", "evidence_bundle", "rag")
KOREAN_JARGON = ("영수증",)
_JARGON = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(sorted(map(re.escape, JARGON_WORDS), key=len, reverse=True))
                     + r")(?![A-Za-z0-9])", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
# (with a final consonant, without one) — "으로/로" also uses "로" after ㄹ.
_PARTICLES = (("으로", "로"), ("은", "는"), ("이", "가"), ("을", "를"), ("과", "와"))


def _hangul(char):
    return "가" <= char <= "힣"


def _final(label):
    """`None` (vowel), `"ㄹ"` or `"other"` for the last Hangul syllable; `"unknown"` if the label ends otherwise."""
    if not label or not _hangul(label[-1]):
        return "unknown"
    final = (ord(label[-1]) - 0xAC00) % 28
    return None if final == 0 else "ㄹ" if final == 8 else "other"


def _particle(label, rest):
    """`(written, replacement)` for the particle right after a replaced token, or `None` if there is none."""
    final = _final(label)
    if final == "unknown":
        return None
    for consonant, vowel in _PARTICLES:
        for written in (consonant, vowel):
            if not rest.startswith(written) or (consonant == "이" and len(rest) > 1 and _hangul(rest[1])):
                continue
            if consonant == "으로":
                return written, vowel if final in {None, "ㄹ"} else consonant
            return written, consonant if final is not None else vowel
    return None


def _label(token, labels):
    if token.startswith("TSEG"):
        return TARGET_LABEL
    return labels.get(token)


def _clean_sentence(sentence, labels):
    """The sentence with known refs replaced, or `None` if it keeps an unknown ref or an internal word."""
    if _JARGON.search(sentence) or any(word in sentence for word in KOREAN_JARGON):
        return None
    out, cursor = [], 0
    for match in _TOKEN.finditer(sentence):
        if match.start() < cursor:
            continue
        label = _label(match.group(1), labels)
        if label is None:
            return None
        out.append(sentence[cursor:match.start()])
        out.append(label)
        cursor = match.end()
        fixed = _particle(label, sentence[cursor:])
        if fixed is not None:
            out.append(fixed[1])
            cursor += len(fixed[0])
    out.append(sentence[cursor:])
    return "".join(out)


def clean_author_text(text, labels):
    """`text` unchanged when it holds no ref or internal word; otherwise the cleaned text ('' if nothing is left)."""
    if not isinstance(text, str) or not (_TOKEN.search(text) or _JARGON.search(text)
                                         or any(word in text for word in KOREAN_JARGON)):
        return text
    kept = []
    for sentence in _SENTENCE_END.split(text.strip()):
        cleaned = _clean_sentence(sentence, labels)
        if cleaned is not None and cleaned.strip():
            kept.append(cleaned.strip())
    return " ".join(kept)


def _source_label(kind, context):
    if kind == "SETTING_BOOK":
        return BOOK_LABEL
    episode = (context or {}).get("episodeNo")
    if kind == "EPISODE" and type(episode) is int:
        return f"{episode}화"
    return None


def author_labels(*, evidence=(), source_catalogue=(), settings=(), subjects=None):
    """Ref → author word for one question: T1, its evidence E refs, catalogue R refs, setting B refs, subject S refs.

    S refs read the SUBJECTS catalogue; without a catalogue every S ref is "대상", with one an S ref it does not
    list is unknown (its sentence is dropped).
    """
    labels = {"T1": TARGET_LABEL}
    for row in source_catalogue or ():
        label = _source_label(row.get("source_kind"), row)
        if label is not None and isinstance(row.get("source_ref"), str):
            labels[row["source_ref"]] = label
    for row in evidence or ():
        label = _source_label(row.get("source_kind"), row.get("source_context"))
        if label is not None:
            labels[row["ref"]] = label
    for row in settings or ():
        labels[row["entry_ref"]] = SETTING_LABEL
    if subjects is None:
        return _SubjectFallback(labels)
    for row in subjects:
        name = row.get("name")
        labels[row["subject_ref"]] = name.strip() if isinstance(name, str) and name.strip() else SUBJECT_FALLBACK
    return labels


class _SubjectFallback(dict):
    """Labels without a SUBJECTS catalogue: any S ref reads "대상"."""

    def get(self, key, default=None):
        if key not in self and re.fullmatch(r"S[1-9]\d*", key):
            return SUBJECT_FALLBACK
        return super().get(key, default)


def sanitize_decision(decision, labels, stats=None):
    """Clean `reason`, `impact_reason` and each disagreement reason; count rewritten fields as AUTHOR_TEXT_SANITIZED."""
    changed = 0

    def clean(text, fallback):
        nonlocal changed
        cleaned = clean_author_text(text, labels)
        if cleaned != text:
            changed += 1
            return cleaned or fallback
        return text

    updates = {"reason": clean(decision.reason, REASON_FALLBACKS.get(decision.outcome, REASON_FALLBACKS["NEEDS_CONTEXT"]))}
    if decision.impact_reason is not None:
        updates["impact_reason"] = clean(decision.impact_reason,
                                         IMPACT_REASON_FALLBACKS.get(decision.impact, IMPACT_REASON_FALLBACKS["REFERENCE"]))
    disagreements = [row.model_copy(update={"reason": clean(row.reason, DISAGREEMENT_FALLBACK)})
                     for row in decision.disagreements]
    if not changed:
        return decision
    if stats is not None:
        stats[COUNTER] += changed
    return decision.model_copy(update={**updates, "disagreements": disagreements})
