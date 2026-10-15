from __future__ import annotations

from typing import Any, TypedDict

from ..models import LawyerProfile, Matter


class CaseAssertionContext(TypedDict):
    assertion_id: str
    kind: str
    text: str
    source_label: str
    confirmed: bool


class AuthorityContext(TypedDict):
    authority_id: str
    citation: str
    proposition: str
    source_url: str
    verified: bool


class PerspectiveContext(TypedDict):
    thesis: str
    priorities: list[str]
    risks: list[str]
    approved: bool


class ApprovedTraitContext(TypedDict):
    trait_id: str
    category: str
    statement: str
    confidence: float


class MatterContext(TypedDict):
    matter_id: str
    title: str
    assertions: list[CaseAssertionContext]
    authorities: list[AuthorityContext]
    case_metadata: dict[str, Any] | None
    claims: list[dict[str, Any]]
    opposing_positions: list[dict[str, Any]]
    argument_plan: dict[str, Any] | None


class ProfileContext(TypedDict):
    profile_id: str
    version: int
    approved_traits: list[ApprovedTraitContext]
    operational_rules: dict[str, Any]


class CitationSyntax(TypedDict):
    case_record: str
    authority: str


class WritingContract(TypedDict):
    required_paragraph_functions: list[str]
    forbidden: list[str]
    citation_syntax: CitationSyntax


class DraftingContext(TypedDict):
    matter: MatterContext
    selected_perspective: PerspectiveContext
    lawyer_profile: ProfileContext
    writing_contract: WritingContract
    instruction: str


WRITING_CONTRACT: WritingContract = {
    "required_paragraph_functions": [
        "fact",
        "evidence_link",
        "legal_rule",
        "application",
        "rebuttal",
        "conclusion",
    ],
    "forbidden": [
        "unsupported_fact",
        "unverified_authority",
        "decorative_prose",
        "emotional_emphasis",
        "semantic_repetition",
    ],
    "citation_syntax": {
        "case_record": "[근거: ASSERTION_ID]",
        "authority": "[법률: AUTHORITY_ID]",
    },
}


def prepare_context(profile: LawyerProfile, matter: Matter) -> DraftingContext:
    if matter.perspective is None or not matter.perspective.approved:
        raise ValueError("A lawyer-approved perspective is required before drafting")

    confirmed_assertions = [item for item in matter.assertions if item.confirmed]
    verified_authorities = [item for item in matter.authorities if item.verified]
    return {
        "matter": {
            "matter_id": matter.matter_id,
            "title": matter.title,
            "assertions": [item.__dict__ for item in confirmed_assertions],
            "authorities": [item.__dict__ for item in verified_authorities],
            "case_metadata": matter.case_metadata.__dict__
            if matter.case_metadata
            else None,
            "claims": [
                {
                    **claim.__dict__,
                    "elements": [element.__dict__ for element in claim.elements],
                }
                for claim in matter.claims
            ],
            "opposing_positions": [item.__dict__ for item in matter.opposing_positions],
            "argument_plan": {
                **matter.argument_plan.__dict__,
                "units": [unit.__dict__ for unit in matter.argument_plan.units],
            }
            if matter.argument_plan
            else None,
        },
        "selected_perspective": matter.perspective.__dict__,
        "lawyer_profile": {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "approved_traits": [
                {
                    "trait_id": trait.trait_id,
                    "category": trait.category,
                    "statement": trait.statement,
                    "confidence": trait.confidence,
                }
                for trait in profile.traits
                if trait.approved
            ],
            "operational_rules": profile.operational_rules,
        },
        "writing_contract": WRITING_CONTRACT,
        "instruction": (
            "확인된 자료만 사용해 준비서면 초안을 작성하십시오. 각 실질 문단에는 제공된 문법으로 "
            "근거를 표시하고, 자료가 부족한 내용은 추측하지 말고 [확인 필요]로 남기십시오."
        ),
    }
