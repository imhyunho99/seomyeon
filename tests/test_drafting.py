import unittest

from seomyeon.models import (
    Authority,
    CaseAssertion,
    LawyerProfile,
    Matter,
    Perspective,
    ProfileTrait,
    SourceSpan,
)
from seomyeon.services.drafting import prepare_context


def _profile() -> LawyerProfile:
    evidence = SourceSpan(
        "brief_1", "준비서면", "따라서 청구는 기각되어야 합니다.", "char:0-18"
    )
    return LawyerProfile(
        profile_id="profile_1",
        lawyer_id="lawyer_1",
        version=1,
        traits=[
            ProfileTrait(
                "trait_1", "structure", "결론을 먼저 제시합니다.", 0.8, [evidence], True
            ),
            ProfileTrait(
                "trait_2", "style", "긴 문장을 씁니다.", 0.7, [evidence], False
            ),
        ],
    )


class DraftingTests(unittest.TestCase):
    def test_context_only_contains_confirmed_and_verified_material(self) -> None:
        matter = Matter(
            matter_id="matter_1",
            lawyer_id="lawyer_1",
            title="대여금 사건",
            assertions=[
                CaseAssertion("F1", "fact", "송금이 이루어졌다.", "을 제1호증", True),
                CaseAssertion(
                    "F2", "disputed_fact", "변제가 없었다.", "상대방 주장", False
                ),
            ],
            authorities=[
                Authority(
                    "A1", "민법 제598조", "소비대차의 의의", "https://law.go.kr", True
                ),
                Authority("A2", "가공 판례", "검증되지 않은 명제", "", False),
            ],
            perspective=Perspective(
                "계약 성립 부정", ["계약서 부재"], ["송금 사실"], True
            ),
        )
        context = prepare_context(_profile(), matter)
        self.assertEqual(
            [item["assertion_id"] for item in context["matter"]["assertions"]], ["F1"]
        )
        self.assertEqual(
            [item["authority_id"] for item in context["matter"]["authorities"]], ["A1"]
        )
        self.assertEqual(
            [item["trait_id"] for item in context["lawyer_profile"]["approved_traits"]],
            ["trait_1"],
        )

    def test_context_requires_lawyer_approved_perspective(self) -> None:
        matter = Matter(
            matter_id="matter_1",
            lawyer_id="lawyer_1",
            title="대여금 사건",
            perspective=Perspective("계약 성립 부정", [], [], False),
        )
        with self.assertRaisesRegex(ValueError, "approved perspective"):
            prepare_context(_profile(), matter)
