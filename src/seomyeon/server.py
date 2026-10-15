from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, TypedDict

from mcp.server import MCPServer

from .models import (
    Authority,
    CaseAssertion,
    Draft,
    DraftingPacket,
    LawyerProfile,
    Matter,
    Perspective,
    PriorBrief,
    ProfileTrait,
    VerificationFinding,
    hash_context,
    new_id,
    to_dict,
    validate_source_id,
)
from .services.drafting import DraftingContext, prepare_context
from .services.profile import add_candidate_trait, build_profile
from .services.verification import verify_draft
from .storage import JsonStore, NotFoundError

DATA_PATH = Path(os.environ.get("SEOMYEON_DATA_PATH", "data/seomyeon.json"))
WORKSPACE_ID = os.environ.get("SEOMYEON_WORKSPACE_ID")
store = JsonStore(DATA_PATH)
mcp = MCPServer("Seomyeon Korean Legal Writer", version="0.1.0")


class BriefRegistration(TypedDict):
    brief_id: str
    lawyer_id: str
    title: str


class BriefHandle(TypedDict):
    brief_id: str
    title: str
    resource_uri: str


class BriefList(TypedDict):
    briefs: list[BriefHandle]


class MatterRegistration(TypedDict):
    matter_id: str
    lawyer_id: str
    title: str


class DraftVerification(TypedDict):
    draft_id: str
    deterministic_passed: bool
    filing_ready: Literal[False]
    requires_human_source_review: Literal[True]
    findings: list[VerificationFinding]


class PreparedDraftingContext(DraftingContext):
    packet_id: str


def _require_workspace(lawyer_id: str) -> None:
    if not WORKSPACE_ID:
        raise RuntimeError("SEOMYEON_WORKSPACE_ID must be configured")
    if lawyer_id != WORKSPACE_ID:
        raise PermissionError("The requested lawyer is outside this server workspace")


@mcp.tool(structured_output=True)
def register_prior_brief(lawyer_id: str, title: str, text: str) -> BriefRegistration:
    """Register one redacted prior brief. Do not send live client secrets in the pilot."""
    _require_workspace(lawyer_id)
    brief = PriorBrief(
        brief_id=new_id("brief"), lawyer_id=lawyer_id, title=title, text=text
    )
    store.save_brief(brief)
    return {"brief_id": brief.brief_id, "lawyer_id": lawyer_id, "title": title}


@mcp.tool(structured_output=True)
def list_prior_briefs(lawyer_id: str) -> BriefList:
    """List prior-brief handles and resource URIs without returning full document text."""
    _require_workspace(lawyer_id)
    briefs = store.list_briefs(lawyer_id)
    return {
        "briefs": [
            {
                "brief_id": brief.brief_id,
                "title": brief.title,
                "resource_uri": f"brief://{lawyer_id}/{brief.brief_id}",
            }
            for brief in briefs
        ]
    }


@mcp.tool(structured_output=True)
def build_lawyer_profile(lawyer_id: str) -> LawyerProfile:
    """Extract observable writing/reasoning traits with exact evidence spans."""
    _require_workspace(lawyer_id)
    briefs = store.list_briefs(lawyer_id)
    try:
        version = store.latest_profile(lawyer_id).version + 1
    except NotFoundError:
        version = 1
    profile = build_profile(lawyer_id, briefs, version)
    store.save_profile(profile)
    return profile


@mcp.tool(structured_output=True)
def add_profile_trait_candidate(
    lawyer_id: str,
    brief_id: str,
    category: Literal["reasoning", "structure", "evidence", "style"],
    statement: str,
    confidence: float,
    evidence_excerpt: str,
) -> ProfileTrait:
    """Add an unapproved profile candidate backed by a verbatim prior-brief excerpt."""
    _require_workspace(lawyer_id)
    brief = store.get_brief(brief_id, lawyer_id)
    profile = store.update_latest_profile(
        lawyer_id,
        lambda current: add_candidate_trait(
            profile=current,
            brief=brief,
            category=category,
            statement=statement,
            confidence=confidence,
            excerpt=evidence_excerpt,
        ),
    )
    return profile.traits[-1]


@mcp.tool(structured_output=True)
def create_matter(lawyer_id: str, title: str) -> MatterRegistration:
    """Create an isolated pilot matter for a lawyer."""
    _require_workspace(lawyer_id)
    matter = Matter(matter_id=new_id("matter"), lawyer_id=lawyer_id, title=title)
    store.save_matter(matter)
    return {"matter_id": matter.matter_id, "lawyer_id": lawyer_id, "title": title}


@mcp.tool(structured_output=True)
def add_case_assertion(
    lawyer_id: str,
    matter_id: str,
    assertion_id: str,
    kind: Literal["fact", "disputed_fact", "request", "constraint"],
    text: str,
    source_label: str,
) -> CaseAssertion:
    """Add an unconfirmed source-labelled assertion for external human review."""
    _require_workspace(lawyer_id)
    validate_source_id(assertion_id)
    assertion = CaseAssertion(
        assertion_id=assertion_id,
        kind=kind,
        text=text,
        source_label=source_label,
        confirmed=False,
    )

    def append_assertion(matter: Matter) -> Matter:
        if any(item.assertion_id == assertion_id for item in matter.assertions):
            raise ValueError(f"Duplicate assertion_id: {assertion_id}")
        matter.assertions.append(assertion)
        return matter

    store.update_matter(matter_id, lawyer_id, append_assertion)
    return assertion


@mcp.tool(structured_output=True)
def add_legal_authority(
    lawyer_id: str,
    matter_id: str,
    authority_id: str,
    citation: str,
    proposition: str,
    source_url: str,
) -> Authority:
    """Add an unverified authority candidate for external human/source review."""
    _require_workspace(lawyer_id)
    validate_source_id(authority_id)
    authority = Authority(
        authority_id=authority_id,
        citation=citation,
        proposition=proposition,
        source_url=source_url,
        verified=False,
    )

    def append_authority(matter: Matter) -> Matter:
        if any(item.authority_id == authority_id for item in matter.authorities):
            raise ValueError(f"Duplicate authority_id: {authority_id}")
        matter.authorities.append(authority)
        return matter

    store.update_matter(matter_id, lawyer_id, append_authority)
    return authority


@mcp.tool(structured_output=True)
def select_case_perspective(
    lawyer_id: str,
    matter_id: str,
    thesis: str,
    priorities: list[str],
    risks: list[str],
) -> Perspective:
    """Store an unapproved perspective candidate for external lawyer review."""
    _require_workspace(lawyer_id)
    perspective = Perspective(
        thesis=thesis,
        priorities=priorities,
        risks=risks,
        approved=False,
    )

    def set_perspective(matter: Matter) -> Matter:
        matter.perspective = perspective
        return matter

    store.update_matter(matter_id, lawyer_id, set_perspective)
    return perspective


@mcp.tool(structured_output=True)
def prepare_drafting_context(lawyer_id: str, matter_id: str) -> PreparedDraftingContext:
    """Return the locked, grounded packet the host model must use to draft."""
    _require_workspace(lawyer_id)
    profile = store.latest_profile(lawyer_id)
    matter = store.get_matter(matter_id, lawyer_id)
    context = prepare_context(profile, matter)
    packet = DraftingPacket(
        packet_id=new_id("packet"),
        lawyer_id=lawyer_id,
        matter_id=matter_id,
        profile_id=profile.profile_id,
        context_hash=hash_context(context),
        context=context,
    )
    store.save_packet(packet)
    return {"packet_id": packet.packet_id, **context}


@mcp.tool(structured_output=True)
def save_and_verify_draft(
    lawyer_id: str, matter_id: str, packet_id: str, title: str, content: str
) -> DraftVerification:
    """Persist a draft and return deterministic citation/concision findings."""
    _require_workspace(lawyer_id)
    packet = store.get_packet(packet_id, lawyer_id)
    if packet.matter_id != matter_id:
        raise ValueError("Drafting packet does not match matter_id")
    matter_context = packet.context["matter"]
    matter = Matter(
        matter_id=packet.matter_id,
        lawyer_id=lawyer_id,
        title=matter_context["title"],
        assertions=[CaseAssertion(**item) for item in matter_context["assertions"]],
        authorities=[Authority(**item) for item in matter_context["authorities"]],
        perspective=Perspective(**packet.context["selected_perspective"]),
    )
    findings = verify_draft(content, matter)
    draft = Draft(
        draft_id=new_id("draft"),
        matter_id=matter_id,
        profile_id=packet.profile_id,
        packet_id=packet.packet_id,
        title=title,
        content=content,
        findings=findings,
    )
    store.save_draft(draft, lawyer_id)
    return {
        "draft_id": draft.draft_id,
        "deterministic_passed": not any(item.severity == "high" for item in findings),
        "filing_ready": False,
        "requires_human_source_review": True,
        "findings": findings,
    }


@mcp.resource("lawyer://{lawyer_id}/profile", mime_type="application/json")
def lawyer_profile_resource(lawyer_id: str) -> str:
    """Read the latest evidence-backed profile."""
    _require_workspace(lawyer_id)
    return json.dumps(
        to_dict(store.latest_profile(lawyer_id)), ensure_ascii=False, indent=2
    )


@mcp.resource("brief://{lawyer_id}/{brief_id}", mime_type="application/json")
def prior_brief_resource(lawyer_id: str, brief_id: str) -> str:
    """Read one lawyer-owned, redacted prior brief for evidence-backed analysis."""
    _require_workspace(lawyer_id)
    return json.dumps(
        to_dict(store.get_brief(brief_id, lawyer_id)), ensure_ascii=False, indent=2
    )


@mcp.resource("case://{lawyer_id}/{matter_id}", mime_type="application/json")
def matter_resource(lawyer_id: str, matter_id: str) -> str:
    """Read one lawyer-owned matter."""
    _require_workspace(lawyer_id)
    return json.dumps(
        to_dict(store.get_matter(matter_id, lawyer_id)), ensure_ascii=False, indent=2
    )


@mcp.resource("draft://{lawyer_id}/{draft_id}", mime_type="application/json")
def draft_resource(lawyer_id: str, draft_id: str) -> str:
    """Read a stored draft and its findings."""
    _require_workspace(lawyer_id)
    return json.dumps(
        to_dict(store.get_draft(draft_id, lawyer_id)), ensure_ascii=False, indent=2
    )


@mcp.resource("packet://{lawyer_id}/{packet_id}", mime_type="application/json")
def drafting_packet_resource(lawyer_id: str, packet_id: str) -> str:
    """Read the immutable, integrity-checked inputs bound to a draft."""
    _require_workspace(lawyer_id)
    return json.dumps(
        to_dict(store.get_packet(packet_id, lawyer_id)), ensure_ascii=False, indent=2
    )


def main() -> None:
    if not WORKSPACE_ID:
        raise RuntimeError("SEOMYEON_WORKSPACE_ID must be configured")
    transport = os.environ.get("SEOMYEON_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("SEOMYEON_TRANSPORT must be stdio or streamable-http")
    if (
        transport == "streamable-http"
        and os.environ.get("SEOMYEON_ALLOW_INSECURE_HTTP") != "1"
    ):
        raise RuntimeError(
            "Pilot HTTP has no authentication; set SEOMYEON_ALLOW_INSECURE_HTTP=1 "
            "only for local testing"
        )
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
