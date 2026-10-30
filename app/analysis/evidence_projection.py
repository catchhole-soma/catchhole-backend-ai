"""Provider-only projection of frozen evidence; never mutate stored inputs."""


def evidence_offset_issue(span) -> str | None:
    if isinstance(span, dict):
        start = span.get("startOffset", span.get("start_offset"))
        end = span.get("endOffset", span.get("end_offset"))
        quote = span.get("quote")
    else:
        start, end = getattr(span, "start_offset", None), getattr(span, "end_offset", None)
        quote = getattr(span, "quote", None)
    if start is None and end is None:
        return "OFFSETS_UNAVAILABLE"
    if (type(start) is not int or type(end) is not int or start < 0 or end <= start
            or not isinstance(quote, str) or not quote.strip() or end - start != len(quote)):
        return "INVALID_OFFSETS"
    # This is an offset-shape check, not a new claim of source verification.
    # Exact frozen-source validation remains the server's responsibility.
    return None


def located_evidence_quotes(spans) -> list[str]:
    return [span["quote"] if isinstance(span, dict) else span.quote
            for span in spans if evidence_offset_issue(span) is None]
