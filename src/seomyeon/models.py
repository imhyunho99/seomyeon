from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Literal
from uuid import uuid4

SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def validate_source_id(value: str) -> str:
    if not SOURCE_ID_RE.fullmatch(value):
        raise ValueError(
            "Source IDs must be 1-64 ASCII letters, digits, underscores, or hyphens"
        )
    return value


def hash_context(context: dict[str, Any]) -> str:
    canonical = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceSpan:
    source_id: str
    label: str
    excerpt: str
    locator: str


@dataclass(frozen=True)
class ProfileTrait:
    trait_id: str
    category: Literal["reasoning", "structure", "evidence", "style", "guardrail"]
    statement: str
    confidence: float
    evidence: list[SourceSpan]
    approved: bool = False


@dataclass
class PriorBrief:
    brief_id: str
    lawyer_id: str
    title: str
    text: str
    created_at: str = field(default_factory=utc_now)


@dataclass
class LawyerProfile:
    profile_id: str
    lawyer_id: str
    version: int
    traits: list[ProfileTrait]
    operational_rules: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class CaseAssertion:
    assertion_id: str
    kind: Literal["fact", "disputed_fact", "request", "constraint"]
    text: str
    source_label: str
    confirmed: bool


@dataclass(frozen=True)
class Authority:
    authority_id: str
    citation: str
    proposition: str
    source_url: str
    verified: bool
    official_source: str | None = None
    official_id: str | None = None
    retrieved_at: str | None = None
    content_hash: str | None = None


@dataclass(frozen=True)
class Perspective:
    thesis: str
    priorities: list[str]
    risks: list[str]
    approved: bool


@dataclass(frozen=True)
class CaseMetadata:
    court: str
    case_number: str
    document_type: str
    filing_party: str
    opposing_party: str
    relief_sought: str


@dataclass(frozen=True)
class LegalElement:
    element_id: str
    name: str
    legal_test: str
    burden_party: str
    conclusion: Literal["satisfied", "not_satisfied", "disputed"]
    assertion_ids: list[str]
    authority_ids: list[str]


@dataclass(frozen=True)
class ClaimTheory:
    claim_id: str
    label: str
    position: Literal["claim", "defense"]
    requested_relief: str
    elements: list[LegalElement]


@dataclass(frozen=True)
class OpposingPosition:
    position_id: str
    allegation: str
    response: Literal["admit", "deny", "lack_knowledge", "partial"]
    response_text: str
    assertion_ids: list[str]
    authority_ids: list[str]


@dataclass(frozen=True)
class ArgumentUnit:
    unit_id: str
    function: Literal[
        "issue",
        "legal_rule",
        "burden",
        "application",
        "rebuttal",
        "conclusion",
    ]
    heading: str
    proposition: str
    assertion_ids: list[str]
    authority_ids: list[str]


@dataclass(frozen=True)
class ArgumentPlan:
    units: list[ArgumentUnit]
    unresolved: list[str]
    approved: bool


@dataclass
class Matter:
    matter_id: str
    lawyer_id: str
    title: str
    assertions: list[CaseAssertion] = field(default_factory=list)
    authorities: list[Authority] = field(default_factory=list)
    perspective: Perspective | None = None
    case_metadata: CaseMetadata | None = None
    claims: list[ClaimTheory] = field(default_factory=list)
    opposing_positions: list[OpposingPosition] = field(default_factory=list)
    argument_plan: ArgumentPlan | None = None
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class VerificationFinding:
    code: str
    severity: Literal["high", "medium", "low"]
    message: str
    locator: str


@dataclass
class Draft:
    draft_id: str
    matter_id: str
    profile_id: str
    packet_id: str
    title: str
    content: str
    findings: list[VerificationFinding]
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class DraftingPacket:
    packet_id: str
    lawyer_id: str
    matter_id: str
    profile_id: str
    context_hash: str
    context: dict[str, Any]
    created_at: str = field(default_factory=utc_now)


def to_dict(value: Any) -> dict[str, Any]:
    return asdict(value)
