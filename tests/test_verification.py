import unittest

from seomyeon.models import Authority, CaseAssertion, Matter
from seomyeon.services.verification import verify_draft


def _matter() -> Matter:
    return Matter(
        matter_id="matter_1",
        lawyer_id="lawyer_1",
        title="대여금 사건",
        assertions=[
            CaseAssertion("F1", "fact", "원고가 송금했다.", "을 제1호증", True)
        ],
        authorities=[
            Authority("A1", "민법 제598조", "소비대차", "https://law.go.kr", True),
            Authority("A2", "대법원 판결", "검증 전", "", False),
        ],
    )


class VerificationTests(unittest.TestCase):
    def test_empty_draft_is_rejected(self) -> None:
        findings = verify_draft("  \n", _matter())
        self.assertEqual(findings[0].code, "empty_draft")
        self.assertEqual(findings[0].severity, "high")

    def test_clean_grounded_draft_passes(self) -> None:
        content = (
            "1. 결론\n\n"
            "원고가 피고에게 금원을 송금한 사실은 확인됩니다. [근거: F1]\n\n"
            "그러나 송금만으로 소비대차 합의가 증명되는 것은 아닙니다. [법률: A1]"
        )
        findings = verify_draft(content, _matter())
        self.assertFalse([item for item in findings if item.severity == "high"])

    def test_unknown_sources_and_fluff_are_flagged(self) -> None:
        content = (
            "누가 보더라도 너무나 명백히 원고의 주장은 부당합니다. [근거: F999]\n\n"
            "등록되지 않은 판례에 따르면 청구는 기각되어야 합니다. [법률: A999]"
        )
        codes = {item.code for item in verify_draft(content, _matter())}
        self.assertIn("emotional_emphasis", codes)
        self.assertIn("unsupported_certainty", codes)
        self.assertIn("unknown_case_source", codes)
        self.assertIn("unknown_authority", codes)

    def test_uncited_material_paragraph_is_high_severity(self) -> None:
        content = "피고는 계약 체결 사실을 일관되게 부인하고 있으며 이를 뒷받침하는 자료도 전혀 제출되지 않았습니다."
        findings = verify_draft(content, _matter())
        self.assertTrue(
            any(
                item.code == "missing_source" and item.severity == "high"
                for item in findings
            )
        )

    def test_short_uncited_sentence_is_rejected(self) -> None:
        findings = verify_draft("청구는 이유 없습니다.", _matter())
        self.assertIn("missing_source", {item.code for item in findings})

    def test_heading_and_uncited_body_are_not_treated_as_heading_only(self) -> None:
        findings = verify_draft("1. 결론\n청구를 기각하여야 합니다.", _matter())
        self.assertIn("missing_source", {item.code for item in findings})

    def test_numbered_factual_sentence_is_not_treated_as_heading(self) -> None:
        findings = verify_draft(
            "1. 원고는 피고에게 10억원을 지급하였습니다.", _matter()
        )
        self.assertIn("missing_source", {item.code for item in findings})

    def test_semantically_unrelated_citation_is_rejected(self) -> None:
        findings = verify_draft(
            "피고는 교통사고 과실비율을 다투고 있습니다. [법률: A1]",
            _matter(),
        )
        self.assertIn("citation_unrelated", {item.code for item in findings})
