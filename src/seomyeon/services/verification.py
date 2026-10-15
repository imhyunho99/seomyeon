from __future__ import annotations

import re

from ..models import Matter, VerificationFinding

CITATION_RE = re.compile(r"\[(근거|법률):\s*([A-Za-z0-9_-]+)\]")
HEADING_ONLY_RE = re.compile(
    r"^\s*(?:[IVX]+\.|[0-9]+\.|[가-힣]+[.)])\s*"
    r"(?:결론|사실관계|확인된 사실관계|다툼 있는 사실|청구취지|쟁점|법리|적용|주장|판단|입증계획|입증책임|사실의 포섭|서론|검토가 필요한 사항|상대방 주장에 대한 (?:인정|부인|부지|일부 인정·일부 부인))"
    r"(?:\s*[—:–-]\s*[^\n.!?]{1,100})?\s*$"
)
DOCUMENT_METADATA_RE = re.compile(
    r"^[^\n]{1,200}\n[^\n]{1,100}\n사건번호:\s*[^\n]*\n"
    r"작성 당사자:\s*[^\n]+\n상대방:\s*[^\n]+\n청구취지:\s*[^\n]+$"
)
FLUFF_PATTERNS = {
    "emotional_emphasis": re.compile(
        r"(너무나|도저히|누가 보더라도|상식적으로 도저히|실로|가히)"
    ),
    "empty_preface": re.compile(
        r"(진실을 밝히기 위하여|아래와 같이 말씀드리고자|살펴보건대)"
    ),
    "unsupported_certainty": re.compile(r"(명백히|당연히|의문의 여지 없이)"),
}


def _normalized(text: str) -> str:
    return re.sub(r"\W+", "", text).lower()


def _terms(text: str) -> set[str]:
    stop = {
        "그리고",
        "그러나",
        "따라서",
        "위하여",
        "대하여",
        "의하여",
        "한다",
        "있다",
        "없다",
        "이는",
        "사실",
        "요건",
        "주장",
    }
    return {
        value
        for value in re.findall(r"[가-힣A-Za-z0-9]{2,}", CITATION_RE.sub("", text))
        if value not in stop
    }


def _has_term_overlap(left: set[str], right: set[str]) -> bool:
    return any(
        first in second
        or second in first
        or (len(first) >= 2 and len(second) >= 2 and first[:2] == second[:2])
        for first in left
        for second in right
    )


def verify_draft(content: str, matter: Matter) -> list[VerificationFinding]:
    findings: list[VerificationFinding] = []
    if not content.strip():
        return [
            VerificationFinding(
                code="empty_draft",
                severity="high",
                message="초안 본문이 비어 있습니다.",
                locator="document",
            )
        ]
    allowed_assertions = {
        item.assertion_id for item in matter.assertions if item.confirmed
    }
    authorities = {item.authority_id: item for item in matter.authorities}
    assertions = {item.assertion_id: item for item in matter.assertions}

    for code, pattern in FLUFF_PATTERNS.items():
        for match in pattern.finditer(content):
            findings.append(
                VerificationFinding(
                    code=code,
                    severity="medium",
                    message=f"간결성 가드레일 위반 표현: {match.group(0)}",
                    locator=f"char:{match.start()}-{match.end()}",
                )
            )

    seen: dict[str, int] = {}
    paragraphs = [
        item.strip() for item in re.split(r"\n\s*\n", content) if item.strip()
    ]
    for index, paragraph in enumerate(paragraphs, start=1):
        normalized = _normalized(CITATION_RE.sub("", paragraph))
        if len(normalized) >= 20 and normalized in seen:
            findings.append(
                VerificationFinding(
                    code="duplicate_paragraph",
                    severity="medium",
                    message=f"문단 {seen[normalized]}과 의미상 동일한 문단이 반복됩니다.",
                    locator=f"paragraph:{index}",
                )
            )
        seen[normalized] = index

        exempt = bool(
            HEADING_ONLY_RE.fullmatch(paragraph)
            or DOCUMENT_METADATA_RE.fullmatch(paragraph)
        )
        citations = CITATION_RE.findall(paragraph)
        if not exempt and not citations:
            findings.append(
                VerificationFinding(
                    code="missing_source",
                    severity="high",
                    message="실질 문단에 사건 기록 또는 법률 근거가 표시되지 않았습니다.",
                    locator=f"paragraph:{index}",
                )
            )
        if citations and not (
            matter.case_metadata
            and paragraph.startswith(matter.case_metadata.relief_sought)
        ):
            source_terms: set[str] = set()
            for kind, source_id in citations:
                if kind == "근거" and source_id in assertions:
                    source_terms |= _terms(
                        f"{assertions[source_id].text} {assertions[source_id].source_label}"
                    )
                if kind == "법률" and source_id in authorities:
                    source_terms |= _terms(
                        f"{authorities[source_id].citation} {authorities[source_id].proposition}"
                    )
            if source_terms and not _has_term_overlap(_terms(paragraph), source_terms):
                findings.append(
                    VerificationFinding(
                        code="citation_unrelated",
                        severity="high",
                        message="문장과 연결된 근거 사이의 직접적인 어휘 연관성을 확인할 수 없습니다.",
                        locator=f"paragraph:{index}",
                    )
                )
    for kind, source_id in CITATION_RE.findall(content):
        if kind == "근거" and source_id not in allowed_assertions:
            findings.append(
                VerificationFinding(
                    code="unknown_case_source",
                    severity="high",
                    message=f"확인되지 않은 사건 근거 ID입니다: {source_id}",
                    locator=f"source:{source_id}",
                )
            )
        if kind == "법률":
            authority = authorities.get(source_id)
            if authority is None:
                findings.append(
                    VerificationFinding(
                        code="unknown_authority",
                        severity="high",
                        message=f"등록되지 않은 법률 근거 ID입니다: {source_id}",
                        locator=f"source:{source_id}",
                    )
                )
            elif not authority.verified:
                findings.append(
                    VerificationFinding(
                        code="unverified_authority",
                        severity="high",
                        message=f"검증되지 않은 법률 근거입니다: {source_id}",
                        locator=f"source:{source_id}",
                    )
                )

    return findings
