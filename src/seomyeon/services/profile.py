from __future__ import annotations

import math
import re
from statistics import mean, median
from collections import Counter
from typing import Literal

from ..models import LawyerProfile, PriorBrief, ProfileTrait, SourceSpan, new_id

SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[가-힣][다요함됨임음].)\s+|(?<=[!?。])\s*")
HEADING_RE = re.compile(
    r"(?m)^\s*((?:[IVX]+\.|[0-9]+\.|[가-힣]\.|[가-힣]+\s*[.)])[^\n]{1,80})$"
)
HEADING_PREFIX_RE = re.compile(r"^\s*(?:[IVX]+\.|[0-9]+\.|[가-힣]\.|[가-힣]+\s*[.)])\s*")
# 청구취지·주문처럼 번호가 붙은 문장은 제목이 아니다
SENTENCE_LIKE_RE = re.compile(r"(?:다|라|요|까|함|음)\s*[.。]?\s*$")
EVIDENCE_RE = re.compile(r"(?:갑|을)\s*제?\s*\d+\s*호증")
CONCLUSION_MARKERS = ("따라서", "그러므로", "결국", "이 사건", "청구")
TRANSITION_MARKERS = (
    "먼저",
    "다음으로",
    "나아가",
    "한편",
    "그러나",
    "반면",
    "특히",
    "요컨대",
    "따라서",
    "결국",
)
FORMAL_ENDING_RE = re.compile(
    r"(합니다|됩니다|입니다|있습니다|없습니다|하였습니다|되었습니다|이다|한다|된다|있다|없다)[.!?]?$"
)


def _operational_rules(
    briefs: list[PriorBrief],
    sentences: list[tuple[PriorBrief, str]],
    average_length: float,
) -> dict[str, object]:
    sentence_values = [sentence for _, sentence in sentences]
    transition_counts = Counter(
        marker
        for sentence in sentence_values
        for marker in TRANSITION_MARKERS
        if marker in sentence
    )
    ending_counts = Counter(
        match.group(1)
        for sentence in sentence_values
        if (match := FORMAL_ENDING_RE.search(sentence))
    )
    conclusion_first_count = 0
    evidence_sentence_count = 0
    for brief in briefs:
        opening = brief.text[: max(200, len(brief.text) // 5)]
        if any(marker in opening for marker in CONCLUSION_MARKERS):
            conclusion_first_count += 1
        evidence_sentence_count += sum(
            1 for sentence in _sentences(brief.text) if EVIDENCE_RE.search(sentence)
        )
    heading_sequence, heading_numbering = _heading_rules(briefs)
    return {
        "sentence_length_target": round(average_length, 1),
        "preferred_transition": transition_counts.most_common(1)[0][0]
        if transition_counts
        else "따라서",
        "transition_vocabulary": [item for item, _ in transition_counts.most_common(5)],
        "preferred_ending": ending_counts.most_common(1)[0][0]
        if ending_counts
        else "합니다",
        "conclusion_first": conclusion_first_count >= max(1, len(briefs) / 2),
        "conclusion_first_ratio": round(conclusion_first_count / len(briefs), 3),
        "evidence_inline_ratio": round(
            evidence_sentence_count / max(1, len(sentence_values)), 3
        ),
        "heading_sequence": heading_sequence,
        "heading_numbering": heading_numbering,
        "sample_size": len(briefs),
    }


def _headings(text: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in HEADING_RE.finditer(text)
        if not SENTENCE_LIKE_RE.search(match.group(1))
    ]


def _numbering_style(heading: str) -> str:
    prefix = HEADING_PREFIX_RE.match(heading).group(0).strip()
    if re.fullmatch(r"[IVX]+\.", prefix):
        return "I."
    if re.fullmatch(r"[0-9]+\.", prefix):
        return "1."
    return "가)" if prefix.endswith(")") else "가."


def _heading_rules(briefs: list[PriorBrief]) -> tuple[list[str], list[str]]:
    """서면 여러 건에 공통으로 나오는 제목과 번호 체계만 규칙으로 남긴다.

    한 서면에만 나오는 제목은 사건 고유 내용(당사자 실명 등)이라 제외한다.
    """
    positions: dict[str, list[float]] = {}
    documents: dict[str, set[str]] = {}
    numbering_orders: Counter[tuple[str, ...]] = Counter()
    for brief in briefs:
        headings = _headings(brief.text)
        if not headings:
            continue
        styles: list[str] = []
        for index, heading in enumerate(headings):
            style = _numbering_style(heading)
            if style not in styles:
                styles.append(style)
            label = re.sub(r"\s+", " ", HEADING_PREFIX_RE.sub("", heading)).strip()
            if len(re.findall(r"[가-힣]", label)) < 2:
                continue
            positions.setdefault(label, []).append(index / len(headings))
            documents.setdefault(label, set()).add(brief.brief_id)
        numbering_orders[tuple(styles)] += 1
    # 같은 사건의 수정본끼리만 공유하는 제목을 거르려면 2건으로는 부족하다
    threshold = max(2, math.ceil(len(briefs) * 0.1)) if len(briefs) >= 2 else 1
    common = [label for label, ids in documents.items() if len(ids) >= threshold]
    common.sort(key=lambda label: median(positions[label]))
    numbering = list(numbering_orders.most_common(1)[0][0]) if numbering_orders else []
    return common[:8], numbering


def _sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or HEADING_RE.fullmatch(line) or re.fullmatch(r"\d+\.", line):
            continue
        sentences.extend(
            sentence.strip()
            for sentence in SENTENCE_BOUNDARY_RE.split(line)
            if sentence.strip()
        )
    return sentences


def _span(brief: PriorBrief, excerpt: str, index: int) -> SourceSpan:
    start = max(index, 0)
    return SourceSpan(
        source_id=brief.brief_id,
        label=brief.title,
        excerpt=excerpt[:240],
        locator=f"char:{start}-{start + len(excerpt)}",
    )


def _first_matching_span(
    briefs: list[PriorBrief], pattern: re.Pattern[str]
) -> SourceSpan | None:
    for brief in briefs:
        match = pattern.search(brief.text)
        if match:
            return _span(brief, match.group(0), match.start())
    return None


def build_profile(
    lawyer_id: str, briefs: list[PriorBrief], version: int
) -> LawyerProfile:
    if not briefs:
        raise ValueError("At least one prior brief is required")

    traits: list[ProfileTrait] = []
    all_sentences = [
        (brief, sentence) for brief in briefs for sentence in _sentences(brief.text)
    ]
    lengths = [len(sentence) for _, sentence in all_sentences]
    average_length = mean(lengths) if lengths else 0
    operational_rules = _operational_rules(briefs, all_sentences, average_length)
    if lengths:
        exemplar_brief, exemplar = min(
            all_sentences, key=lambda item: abs(len(item[1]) - average_length)
        )
        average = round(average_length, 1)
        density = "짧고 압축된" if average <= 70 else "상세한"
        traits.append(
            ProfileTrait(
                trait_id=new_id("trait"),
                category="style",
                statement=f"평균 문장 길이는 약 {average}자로, {density} 문장을 주로 사용합니다.",
                confidence=min(0.95, 0.45 + len(lengths) / 300),
                evidence=[
                    _span(exemplar_brief, exemplar, exemplar_brief.text.find(exemplar))
                ],
            )
        )

    heading_span = next(
        (
            _span(brief, headings[0], brief.text.find(headings[0]))
            for brief in briefs
            if (headings := _headings(brief.text))
        ),
        None,
    )
    heading_count = sum(len(_headings(brief.text)) for brief in briefs)
    if heading_span:
        traits.append(
            ProfileTrait(
                trait_id=new_id("trait"),
                category="structure",
                statement="번호가 붙은 짧은 제목으로 논증 단위를 분리합니다.",
                confidence=min(0.95, 0.5 + heading_count / max(10, len(briefs) * 6)),
                evidence=[heading_span],
            )
        )

    evidence_span = _first_matching_span(briefs, EVIDENCE_RE)
    evidence_count = sum(len(EVIDENCE_RE.findall(brief.text)) for brief in briefs)
    if evidence_span:
        traits.append(
            ProfileTrait(
                trait_id=new_id("trait"),
                category="evidence",
                statement="사실 주장 가까이에 서증 표지를 배치하는 경향이 있습니다.",
                confidence=min(0.95, 0.5 + evidence_count / max(10, len(briefs) * 5)),
                evidence=[evidence_span],
            )
        )

    for brief in briefs:
        opening = brief.text[: max(200, len(brief.text) // 5)]
        marker = next((value for value in CONCLUSION_MARKERS if value in opening), None)
        if marker:
            index = brief.text.find(marker)
            excerpt = brief.text[index : index + 180].split("\n", 1)[0]
            traits.append(
                ProfileTrait(
                    trait_id=new_id("trait"),
                    category="reasoning",
                    statement="문서 앞부분에서 사건의 핵심 결론 또는 청구 방향을 먼저 제시합니다.",
                    confidence=0.65,
                    evidence=[_span(brief, excerpt, index)],
                )
            )
            break

    preferred_transition = str(operational_rules["preferred_transition"])
    supporting_documents: set[str] = set()
    transition_evidence: list[SourceSpan] = []
    for brief, sentence in all_sentences:
        if preferred_transition not in sentence or brief.brief_id in supporting_documents:
            continue
        supporting_documents.add(brief.brief_id)
        if len(transition_evidence) < 3:
            transition_evidence.append(
                _span(brief, sentence, brief.text.find(sentence))
            )
    if len(supporting_documents) >= 2:
        traits.append(
            ProfileTrait(
                trait_id=new_id("trait"),
                category="style",
                statement=(
                    f"논리 전환에서 ‘{preferred_transition}’을 반복적으로 사용합니다."
                ),
                confidence=min(0.95, 0.6 + len(supporting_documents) / 100),
                evidence=transition_evidence,
            )
        )

    traits.append(
        ProfileTrait(
            trait_id=new_id("trait"),
            category="guardrail",
            statement="근거 없는 강조, 감정적 표현, 동일 의미 반복은 개인 문체보다 우선하여 제거합니다.",
            confidence=1.0,
            evidence=[
                SourceSpan(
                    source_id="system-policy",
                    label="파일럿 작성 정책",
                    excerpt="모든 문단은 사실·증거·법리·적용·반박·결론 중 하나의 기능을 가져야 한다.",
                    locator="policy:concise-writing",
                )
            ],
            approved=True,
        )
    )

    return LawyerProfile(
        profile_id=new_id("profile"),
        lawyer_id=lawyer_id,
        version=version,
        traits=traits,
        operational_rules=operational_rules,
    )


def approve_traits(profile: LawyerProfile, trait_ids: set[str]) -> LawyerProfile:
    profile.traits = [
        ProfileTrait(
            trait_id=trait.trait_id,
            category=trait.category,
            statement=trait.statement,
            confidence=trait.confidence,
            evidence=trait.evidence,
            approved=trait.approved or trait.trait_id in trait_ids,
        )
        for trait in profile.traits
    ]
    return profile


def add_candidate_trait(
    profile: LawyerProfile,
    brief: PriorBrief,
    category: Literal["reasoning", "structure", "evidence", "style"],
    statement: str,
    confidence: float,
    excerpt: str,
) -> LawyerProfile:
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    excerpt = excerpt.strip()
    if len(excerpt) < 10:
        raise ValueError("evidence excerpt must contain at least 10 characters")
    index = brief.text.find(excerpt)
    if index < 0:
        raise ValueError(
            "evidence excerpt does not occur verbatim in the selected brief"
        )
    profile.traits.append(
        ProfileTrait(
            trait_id=new_id("trait"),
            category=category,
            statement=statement.strip(),
            confidence=confidence,
            evidence=[_span(brief, excerpt, index)],
            approved=False,
        )
    )
    return profile
