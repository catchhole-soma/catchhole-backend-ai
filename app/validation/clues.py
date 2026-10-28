"""Deterministic hints for independent coverage; hints are never findings or facts."""

import re

PATTERNS = (
    (
        "NUMBER_DATE",
        re.compile(
            r"(?:[0-9]+(?:[.,][0-9]+)?|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|[일이삼사오육칠팔구십백천만]+)\s*(?:년|월|일|시|분|초|살|세|명|개|번|회|권|마리|자루|킬로|미터)"
        ),
    ),
    ("RELATIVE_TIME", re.compile(r"이틀|사흘|나흘|닷새|엿새|일주일|보름")),
    ("WEEKDAY", re.compile(r"[월화수목금토일]요일")),
)


def collect_clues(text, names=()):
    rows = []
    for kind, pattern in PATTERNS:
        rows.extend((match.start(), match.end(), kind) for match in pattern.finditer(text))
    for name in dict.fromkeys(name for name in names if isinstance(name, str) and name):
        matches = list(re.finditer(re.escape(name), text))
        if len(matches) > 1:
            rows.extend((match.start(), match.end(), "REPEATED_NAME") for match in matches)
    return [
        {
            "ref": f"CL{index + 1}",
            "kind": kind,
            "span": {"source_ref": "T1", "start": start, "end": end, "quote": text[start:end]},
        }
        for index, (start, end, kind) in enumerate(sorted(set(rows)))
    ]
