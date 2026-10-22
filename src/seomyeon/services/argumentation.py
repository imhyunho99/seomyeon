from __future__ import annotations

from dataclasses import replace

from ..korean import josa
from ..models import ArgumentPlan, ArgumentUnit, Matter


RESPONSE_LABELS = {
    "admit": "인정",
    "deny": "부인",
    "lack_knowledge": "부지",
    "partial": "일부 인정·일부 부인",
}
CONCLUSION_LABELS = {
    "satisfied": "충족한다",
    "not_satisfied": "충족하지 못한다",
    "disputed": "충족 여부가 다투어진다",
}


def build_argument_plan(matter: Matter) -> ArgumentPlan:
    unresolved: list[str] = []
    units: list[ArgumentUnit] = []
    confirmed = {
        item.assertion_id: item for item in matter.assertions if item.confirmed
    }
    verified = {item.authority_id: item for item in matter.authorities if item.verified}
    metadata = matter.case_metadata
    if metadata is None:
        unresolved.append("법원·사건번호·당사자·문서 유형·청구취지를 입력해야 합니다.")
    else:
        required = {
            "법원": metadata.court,
            "사건번호": metadata.case_number,
            "문서 유형": metadata.document_type,
            "작성 당사자": metadata.filing_party,
            "상대방": metadata.opposing_party,
            "청구취지": metadata.relief_sought,
        }
        unresolved.extend(
            f"{label} 정보가 없습니다."
            for label, value in required.items()
            if not value.strip()
        )
    if not matter.claims:
        unresolved.append("청구 또는 항변과 그 요건사실을 한 개 이상 입력해야 합니다.")

    for claim in matter.claims:
        if not claim.elements:
            unresolved.append(f"{claim.label}: 요건사실이 없습니다.")
            continue
        for element in claim.elements:
            missing_facts = [
                item for item in element.assertion_ids if item not in confirmed
            ]
            missing_law = [
                item for item in element.authority_ids if item not in verified
            ]
            if not element.assertion_ids:
                unresolved.append(
                    f"{claim.label}/{element.name}: 연결된 사건 사실이 없습니다."
                )
            if not element.authority_ids:
                unresolved.append(
                    f"{claim.label}/{element.name}: 연결된 법률 근거가 없습니다."
                )
            if missing_facts:
                unresolved.append(
                    f"{claim.label}/{element.name}: 미승인 사건 근거 {', '.join(missing_facts)}"
                )
            if missing_law:
                unresolved.append(
                    f"{claim.label}/{element.name}: 미승인 법률 근거 {', '.join(missing_law)}"
                )
            units.append(
                ArgumentUnit(
                    unit_id=f"{claim.claim_id}-{element.element_id}-rule",
                    function="legal_rule",
                    heading=f"법리 — {claim.label}: {element.name}",
                    proposition=(
                        f"{element.legal_test} 이 법리는 {element.name} 요건의 "
                        "판단 범위에서만 적용한다."
                    ),
                    assertion_ids=[],
                    authority_ids=element.authority_ids,
                )
            )
            units.append(
                ArgumentUnit(
                    unit_id=f"{claim.claim_id}-{element.element_id}-burden",
                    function="burden",
                    heading="입증책임",
                    proposition=(
                        f"{element.name} 요건은 {josa(element.burden_party, '이', '가')} 입증하여야 한다."
                    ),
                    assertion_ids=[],
                    authority_ids=element.authority_ids,
                )
            )
            fact_text = " ".join(
                confirmed[source_id].text
                for source_id in element.assertion_ids
                if source_id in confirmed
            )
            units.append(
                ArgumentUnit(
                    unit_id=f"{claim.claim_id}-{element.element_id}-application",
                    function="application",
                    heading="사실의 포섭",
                    proposition=(
                        f"{fact_text} 위 사실을 {element.legal_test}에 비추어 보면 "
                        f"{element.name} 요건은 {CONCLUSION_LABELS[element.conclusion]}."
                    ).strip(),
                    assertion_ids=element.assertion_ids,
                    authority_ids=element.authority_ids,
                )
            )

    for position in matter.opposing_positions:
        missing_facts = [
            item for item in position.assertion_ids if item not in confirmed
        ]
        missing_law = [item for item in position.authority_ids if item not in verified]
        if position.response != "admit" and not position.response_text.strip():
            unresolved.append(
                f"상대방 주장 {position.position_id}: 구체적인 답변이 없습니다."
            )
        if not position.assertion_ids and not position.authority_ids:
            unresolved.append(
                f"상대방 주장 {position.position_id}: 연결된 사실 또는 법률 근거가 없습니다."
            )
        if missing_facts or missing_law:
            unresolved.append(
                f"상대방 주장 {position.position_id}: 미승인 근거가 연결되어 있습니다."
            )
        units.append(
            ArgumentUnit(
                unit_id=f"{position.position_id}-rebuttal",
                function="rebuttal",
                heading=f"상대방 주장에 대한 {RESPONSE_LABELS[position.response]}",
                proposition=(
                    f"상대방은 ‘{position.allegation}’라고 주장한다. 이에 대하여 "
                    f"{RESPONSE_LABELS[position.response]}하며, {position.response_text}"
                ).strip(" ,"),
                assertion_ids=position.assertion_ids,
                authority_ids=position.authority_ids,
            )
        )

    if metadata and matter.claims:
        fact_ids = sorted(
            {source_id for unit in units for source_id in unit.assertion_ids}
        )
        authority_ids = sorted(
            {source_id for unit in units for source_id in unit.authority_ids}
        )
        units.append(
            ArgumentUnit(
                unit_id="requested-relief-conclusion",
                function="conclusion",
                heading="결론",
                proposition=metadata.relief_sought,
                assertion_ids=fact_ids,
                authority_ids=authority_ids,
            )
        )
    return ArgumentPlan(units=units, unresolved=unresolved, approved=False)


def approve_argument_plan(plan: ArgumentPlan) -> ArgumentPlan:
    if plan.unresolved:
        raise ValueError("미해결 항목이 있는 논증계획은 승인할 수 없습니다.")
    return replace(plan, approved=True)
