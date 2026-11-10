"""Grade a setting-book live replay: tokens per stage, cost, guard, and source-fact retention.

Synthetic cases are graded automatically from the fixture gold atoms and the expected stage-1
selection. Real cases are matched automatically against a private gold file (subject + value);
unmatched gold facts are listed for manual review and never auto-counted as misses or hits.
Customer text is read from private files only and is never printed by this script.
"""

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

GUARD_LIMIT = 128_000  # v0054 default; the v0053 guard was 64,000
WILDCARD_SUBJECT = "미상"


def norm(value):
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"\s+", "", value.strip().strip("\"'`“”‘’()[]{}<>〈〉《》"))


def cost_usd(usage, prices):
    if prices is None or any(usage.get(k) is None for k in ("input_tokens", "output_tokens")):
        return None
    cached = usage.get("cached_input_tokens") or 0
    uncached = usage["input_tokens"] - cached
    total = (Decimal(uncached) * prices["input"] + Decimal(cached) * prices["cached_input"]
             + Decimal(usage["output_tokens"]) * prices["output"]) / Decimal(1_000_000)
    return float(round(total, 6))


def stage_usage(calls, stage):
    rows = [c for c in calls if c.get("stage", "extraction") == stage]
    if not rows:
        return None

    def total(field):
        values = [c.get(field) for c in rows]
        return None if any(v is None for v in values) else sum(values)

    return {"calls": len(rows), "input_tokens": total("input_tokens"),
            "cached_input_tokens": total("cached_input_tokens"), "output_tokens": total("output_tokens"),
            "guard_tokens_max": max(c.get("guard_tokens") or 0 for c in rows),
            "within_guard": all((c.get("guard_tokens") or 0) <= GUARD_LIMIT for c in rows),
            "seconds": round(sum(c.get("seconds") or 0 for c in rows), 3)}


def total_usage(calls):
    def total(field):
        values = [c.get(field) for c in calls]
        return None if any(v is None for v in values) else sum(values)
    return {"calls": len(calls), "input_tokens": total("input_tokens"),
            "cached_input_tokens": total("cached_input_tokens"), "output_tokens": total("output_tokens"),
            "within_guard": all((c.get("guard_tokens") or 0) <= GUARD_LIMIT for c in calls)}


def value_retained(gold_value, text):
    """Every whitespace-separated token of the gold value appears in the candidate's value text.

    Gold values like `스타테일러 최고전력` may be rendered with other explicit qualifiers in between
    (`스타테일러 국장 직속 12성, 최고전력`); the quote only grounds, the value fields must carry it.
    """
    tokens = [norm(t) for t in str(gold_value).split()]
    tokens = [t for t in tokens if t]
    return bool(tokens) and all(t in text for t in tokens)


def value_text(candidate):
    parts = [candidate.get("attribute_value") or ""]
    if candidate.get("value_json") is not None:
        parts.append(json.dumps(candidate["value_json"], ensure_ascii=False))
    return norm(" ".join(parts))


def subject_matches(candidate, names):
    if WILDCARD_SUBJECT in names:
        return True
    targets = {norm(n) for n in names}
    for field in ("entity_name", "raw_entity_mention"):
        value = norm(candidate.get(field))
        if value and any(value == t or (len(t) >= 2 and t in value) for t in targets):
            return True
    return False


def key_matches(candidate, key):
    attribute = candidate.get("attribute_name") or ""
    if attribute == key:
        return "exact"
    if key and attribute.split(".")[0] == key.split(".")[0]:
        return "family"
    return None


def atom_matches(candidate, atom):
    if atom.get("kind"):
        return candidate.get("candidate_kind") == atom["kind"] and subject_matches(candidate, atom["names"])
    if candidate.get("candidate_kind") != "SETTING" or not subject_matches(candidate, atom["names"]):
        return None
    strength = key_matches(candidate, atom["key"])
    if strength is None:
        return None
    text = value_text(candidate)
    if "value" in atom:
        numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
        if str(atom["value"]) not in numbers:
            return None
    if "value_contains" in atom and norm(atom["value_contains"]) not in text:
        return None
    if "active" in atom:
        value_json = candidate.get("value_json") or {}
        if not isinstance(value_json, dict) or value_json.get("active") is not atom["active"]:
            return None
    if "forecast" in atom:
        forecast = bool((candidate.get("narrative_time") or {}).get("fact_time")) or "예정" in (candidate.get("attribute_value") or "")
        if forecast is not atom["forecast"]:
            return None
    return strength


def selected_count(record, case):
    """Characters whose CURRENT details the extraction request carried."""
    selection = record.get("selection")
    if selection:
        return len(selection["selected_refs"]) + len(selection["ambiguous_refs"])
    if record["variant"] == "before":
        return record.get("catalogue_size", len(case["known"]))  # pre-selection baseline: all details
    # Numeric-row chunks select deterministically without a model call; derive the count from the request
    # when recorded, otherwise recompute the same deterministic selection from the frozen inputs.
    for call in record.get("calls", []):
        if call.get("stage", "extraction") == "extraction" and call.get("selected_characters") is not None:
            return call["selected_characters"]
    return numeric_selection_count(case)


def numeric_selection_count(case):
    from app.analysis.book_character_context import book_character_rows, select_numeric_row_refs
    from app.analysis.ordered_context import OrderedExtractionContext
    from app.analysis.semantic_current_context import SemanticCurrentInput
    from app.schemas.analysis_context import WorkerAnalysisContext
    from app.schemas.worker import WorkerAnalysisKnownCharacterPayload
    known = tuple(WorkerAnalysisKnownCharacterPayload.model_validate(k) for k in case["known"])
    ordered = OrderedExtractionContext(WorkerAnalysisContext.model_validate(case["context"]), known, ())
    catalog, _details, _unresolved = book_character_rows(
        ordered, SemanticCurrentInput(case["semantic"]["characters"], case["semantic"]["worlds"]))
    refs = select_numeric_row_refs(case["body"], catalog)
    return None if refs is None else len(refs)


def grade_synthetic(case, record):
    candidates = record.get("result", {}).get("candidates", [])
    atoms = []
    for index, atom in enumerate(case["gold"]):
        hits = [(i, atom_matches(c, atom)) for i, c in enumerate(candidates)]
        hits = [(i, s) for i, s in hits if s]
        atoms.append({"gold_index": index, "matched": bool(hits),
                      "exact_key": any(s == "exact" or s is True for _, s in hits),
                      "candidate_indices": [i for i, _ in hits]})
    grounding_failures = [i for i, c in enumerate(candidates)
                          for span in c.get("evidence_spans", []) if span.get("quote") and span["quote"] not in case["body"]]
    known = {norm(k["name"]) for k in case["known"]} | {norm(a) for k in case["known"] for a in k["aliases"]}
    body = norm(case["body"])
    foreign_subjects = [i for i, c in enumerate(candidates)
                        if norm(c.get("entity_name")) and norm(c.get("entity_name")) != norm(WILDCARD_SUBJECT)
                        and norm(c.get("entity_name")) not in known
                        and norm(c.get("entity_name")) not in body
                        and not any(norm(n) in norm(c.get("entity_name")) for n in case.get("gold_subject_hints", []))]
    result = {"gold_total": len(atoms), "gold_matched": sum(a["matched"] for a in atoms),
              "gold_exact_key": sum(a["exact_key"] for a in atoms), "atoms": atoms,
              "setting_candidates": sum(c.get("candidate_kind") == "SETTING" for c in candidates),
              "discovery_candidates": sum(c.get("candidate_kind") == "CHARACTER_DISCOVERY" for c in candidates),
              "ungrounded_quote_candidates": sorted(set(grounding_failures)),
              "subjects_outside_catalogue_and_body": foreign_subjects}
    selection = record.get("selection")
    if "expected_selection" in case and record.get("variant") == "after":
        name_by_ref = {f"K{i}": k["name"] for i, k in enumerate(case["known"], 1)}
        got = set()
        if selection:
            got = {name_by_ref.get(r, r) for r in selection["selected_refs"] + selection["ambiguous_refs"]}
        expected = set(case["expected_selection"])
        result["selection"] = {"expected": sorted(expected), "selected_count": len(got),
                               "missing": sorted(expected - got), "extra": sorted(got - expected),
                               "unmatched_mentions": selection["unmatched_mentions"] if selection else None,
                               "expected_unmatched_found": [m for m in case.get("expected_unmatched", [])
                                                             if selection and any(norm(m) in norm(x) for x in selection["unmatched_mentions"])],
                               "method": "model" if selection else ("numeric_or_none")}
    return result


def grade_real(case, record, gold_case):
    """Auto-match private gold facts by subject + value text; everything else goes to manual review."""
    candidates = record.get("result", {}).get("candidates", [])
    matched, review = [], []
    for fact in gold_case["facts"]:
        hits = []
        for i, c in enumerate(candidates):
            if c.get("candidate_kind") != "SETTING" or not subject_matches(c, [fact["subject"]]):
                continue
            text = value_text(c)
            if value_retained(fact["value"], text):
                qualifier = fact.get("qualifier") or {}
                hits.append({"candidate_index": i, "exact_substring": norm(fact["value"]) in text,
                             "qualifier_retained": all(value_retained(v, text) for k, v in qualifier.items()
                                                       if isinstance(v, str) and k != "scope")})
        (matched if hits else review).append({"id": fact["id"], "tier": fact["rubric_tier"], "hits": hits})
    by_tier = defaultdict(lambda: {"total": 0, "auto_matched": 0, "qualifier_retained": 0})
    for row in matched + review:
        tier = by_tier[row["tier"]]
        tier["total"] += 1
        if row["hits"]:
            tier["auto_matched"] += 1
            tier["qualifier_retained"] += any(h["qualifier_retained"] for h in row["hits"])
    grounding_failures = [i for i, c in enumerate(candidates)
                          for span in c.get("evidence_spans", []) if span.get("quote") and span["quote"] not in case["body"]]
    return {"facts_total": len(gold_case["facts"]), "auto_matched": len(matched), "manual_review": [r["id"] for r in review],
            "by_tier": dict(by_tier), "matched_rows": matched,
            "setting_candidates": sum(c.get("candidate_kind") == "SETTING" for c in candidates),
            "discovery_candidates": sum(c.get("candidate_kind") == "CHARACTER_DISCOVERY" for c in candidates),
            "ungrounded_quote_candidates": sorted(set(grounding_failures))}


def main(args):
    cases = {c["case"]: c for c in json.loads(args.inputs.read_text())}
    results = json.loads(args.results.read_text())
    gold = None
    if args.gold:
        gold = {c["case"]: c for c in json.loads(args.gold.read_text())["cases"]}
    prices = None
    if args.price_input is not None:
        prices = {"input": Decimal(str(args.price_input)), "cached_input": Decimal(str(args.price_cached_input)),
                  "output": Decimal(str(args.price_output))}
    rows = []
    for record in sorted(results, key=lambda r: (r["case"], r["variant"], r["repeat"])):
        case = cases[record["case"]]
        calls = record["calls"]
        usage = total_usage(calls)
        row = {"case": record["case"], "variant": record["variant"], "repeat": record["repeat"],
               "status": record["status"], "seconds": record["seconds"],
               "catalogue_size": record.get("catalogue_size", len(case["known"])),
               "selected_characters": selected_count(record, case),
               "stages": {s: stage_usage(calls, s) for s in ("selection", "extraction") if stage_usage(calls, s)},
               "total": usage, "cost_usd": cost_usd(usage, prices)}
        if record["status"] == "SUCCEEDED":
            if gold and record["case"] in gold and gold[record["case"]]["facts"]:
                row["quality"] = grade_real(case, record, gold[record["case"]])
            elif "gold" in case:
                row["quality"] = grade_synthetic(case, record)
        rows.append(row)
    summary = defaultdict(lambda: {"runs": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                                   "seconds": 0.0, "cost_usd": 0.0, "within_guard": True,
                                   "selection_input_tokens": 0, "selection_output_tokens": 0})
    for row in rows:
        agg = summary[row["variant"]]
        agg["runs"] += 1
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            agg[key] += row["total"][key] or 0
        agg["seconds"] = round(agg["seconds"] + row["seconds"], 3)
        agg["cost_usd"] = round(agg["cost_usd"] + (row["cost_usd"] or 0), 6)
        agg["within_guard"] &= row["total"]["within_guard"]
        sel = row["stages"].get("selection")
        if sel:
            agg["selection_input_tokens"] += sel["input_tokens"] or 0
            agg["selection_output_tokens"] += sel["output_tokens"] or 0
    output = {"format_version": 1, "guard_limit": GUARD_LIMIT,
              "prices_usd_per_million": {k: str(v) for k, v in prices.items()} if prices else None,
              "rows": rows, "summary": dict(summary)}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print_table(rows)


def print_table(rows):
    header = ("case", "var", "rep", "status", "cat", "sel", "sel_in", "sel_out", "ext_in", "ext_out", "tot_in", "tot_out",
              "guard_ok", "sec", "usd", "quality")
    print("\t".join(header))
    for row in rows:
        sel = row["stages"].get("selection") or {}
        ext = row["stages"].get("extraction") or {}
        quality = row.get("quality") or {}
        if "gold_total" in quality:
            q = f"gold {quality['gold_matched']}/{quality['gold_total']}"
            if "selection" in quality:
                q += f" selmiss={len(quality['selection']['missing'])} extra={len(quality['selection']['extra'])}"
        elif "facts_total" in quality:
            q = f"auto {quality['auto_matched']}/{quality['facts_total']} review={len(quality['manual_review'])}"
        else:
            q = "-"
        print("\t".join(str(v) for v in (
            row["case"], row["variant"], row["repeat"], row["status"], row["catalogue_size"], row["selected_characters"],
            sel.get("input_tokens", 0), sel.get("output_tokens", 0), ext.get("input_tokens"), ext.get("output_tokens"),
            row["total"]["input_tokens"], row["total"]["output_tokens"], row["total"]["within_guard"], row["seconds"],
            row["cost_usd"], q)))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--gold", type=Path, help="Private audit gold for real cases")
    p.add_argument("--price-input", type=float, help="USD per 1M uncached input tokens")
    p.add_argument("--price-cached-input", type=float, default=0.0)
    p.add_argument("--price-output", type=float, default=0.0)
    main(p.parse_args())
