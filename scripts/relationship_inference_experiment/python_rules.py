"""Declarative bounded rule profile for the Python experiment."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Atom:
    kind: str
    subject: str
    object: str
    subtype: str | None = None
    origin: str = "direct"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    body: tuple[Atom, ...]
    head_kind: str
    head_subject: str
    head_object: str
    derivation_type: str
    generation_gap: int | None = None


BASE_RULES = (
    Rule(
        "R_PARENT_INVERSE",
        (Atom("PARENT_OF", "x", "y"),),
        "CHILD_OF",
        "y",
        "x",
        "SEMANTIC_INVERSE",
    ),
    Rule(
        "R_BIOLOGICAL_GRANDPARENT",
        (Atom("PARENT_OF", "x", "y", "BIOLOGICAL"), Atom("PARENT_OF", "y", "z", "BIOLOGICAL")),
        "GRANDPARENT_OF",
        "x",
        "z",
        "NEW_RELATION",
        2,
    ),
)

GRANDCHILD_RULE = Rule(
    "R_GRANDPARENT_INVERSE",
    (Atom("GRANDPARENT_OF", "x", "y", "BIOLOGICAL", "derived"),),
    "GRANDCHILD_OF",
    "y",
    "x",
    "SEMANTIC_INVERSE",
    2,
)


def rules_for(*, extended: bool = False) -> tuple[Rule, ...]:
    return BASE_RULES + (GRANDCHILD_RULE,) if extended else BASE_RULES
