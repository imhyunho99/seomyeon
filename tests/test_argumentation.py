import unittest

from seomyeon.models import (
    Authority,
    CaseAssertion,
    CaseMetadata,
    ClaimTheory,
    LegalElement,
    Matter,
    OpposingPosition,
)
from seomyeon.services.argumentation import (
    approve_argument_plan,
    build_argument_plan,
)


def _structured_matter() -> Matter:
    return Matter(
        matter_id="matter_1",
        lawyer_id="lawyer_1",
        title="대여금 사건",
        assertions=[
            CaseAssertion(
                "F1", "fact", "원고가 3천만 원을 송금하였다.", "갑 제1호증 2쪽", True
            ),
            CaseAssertion(
                "F2",
                "disputed_fact",
                "피고는 투자금이라고 주장한다.",
                "답변서 3쪽",
                True,
            ),
        ],
        authorities=[
            Authority(
                "A1",
                "민법 제598조",
                "소비대차는 금전 이전과 반환 약정으로 성립한다.",
                "https://law.go.kr",
                True,
            )
        ],
        case_metadata=CaseMetadata(
            "서울중앙지방법원 귀중",
            "2026가단12345",
            "준비서면",
            "피고 홍길동",
            "원고 김철수",
            "원고의 청구를 기각한다.",
        ),
        claims=[
            ClaimTheory(
                "C1",
                "대여금 반환청구",
                "defense",
                "청구 기각",
                [
                    LegalElement(
                        "E1",
                        "반환 약정",
                        "소비대차는 금전 이전과 반환 약정으로 성립한다.",
                        "원고",
                        "not_satisfied",
                        ["F1"],
                        ["A1"],
                    )
                ],
            )
        ],
        opposing_positions=[
            OpposingPosition(
                "P1",
                "송금액은 대여금이다.",
                "deny",
                "송금 사실만으로 반환 약정은 증명되지 않는다.",
                ["F1", "F2"],
                ["A1"],
            )
        ],
    )


class ArgumentationTests(unittest.TestCase):
    def test_builds_element_burden_application_and_rebuttal_units(self) -> None:
        plan = build_argument_plan(_structured_matter())
        self.assertEqual(plan.unresolved, [])
        functions = {item.function for item in plan.units}
        self.assertTrue(
            {"legal_rule", "burden", "application", "rebuttal", "conclusion"}
            <= functions
        )
        approved = approve_argument_plan(plan)
        self.assertTrue(approved.approved)

    def test_missing_evidence_blocks_approval(self) -> None:
        matter = _structured_matter()
        matter.claims[0].elements[0].authority_ids.clear()
        plan = build_argument_plan(matter)
        self.assertTrue(any("법률 근거" in item for item in plan.unresolved))
        with self.assertRaisesRegex(ValueError, "미해결"):
            approve_argument_plan(plan)


if __name__ == "__main__":
    unittest.main()
