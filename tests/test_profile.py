import unittest
from statistics import mean as statistics_mean
from unittest.mock import patch

from seomyeon.models import PriorBrief
from seomyeon.services.profile import add_candidate_trait, approve_traits, build_profile


class ProfileTests(unittest.TestCase):
    def test_one_hundred_synthetic_briefs_recover_dominant_implicit_style(self) -> None:
        briefs = []
        for index in range(100):
            briefs.append(
                PriorBrief(
                    brief_id=f"brief_{index:03d}",
                    lawyer_id="lawyer_1",
                    title=f"합성 준비서면 {index + 1}",
                    text=(
                        "1. 결론\n"
                        f"요컨대 이 사건 제{index + 1} 청구는 이유 없습니다.\n\n"
                        "2. 사실관계\n"
                        f"피고는 계약 체결 사실을 다투고 있습니다(을 제{index % 9 + 1}호증).\n\n"
                        "3. 법리와 적용\n"
                        "나아가 상대방이 제출한 자료만으로는 요건사실이 증명되지 않습니다.\n\n"
                        "4. 결론\n"
                        "요컨대 원고의 청구는 기각되어야 합니다."
                    ),
                )
            )
        profile = build_profile("lawyer_1", briefs, version=1)
        rules = profile.operational_rules
        self.assertEqual(rules["sample_size"], 100)
        self.assertTrue(rules["conclusion_first"])
        self.assertEqual(rules["preferred_transition"], "요컨대")
        self.assertGreater(rules["evidence_inline_ratio"], 0)
        self.assertIn("요컨대", rules["transition_vocabulary"])
        self.assertTrue(any("요컨대" in trait.statement for trait in profile.traits))

    def test_one_hundred_briefs_resist_ten_style_outliers(self) -> None:
        briefs = [
            PriorBrief(
                brief_id=f"brief_{index:03d}",
                lawyer_id="lawyer_1",
                title=f"합성 서면 {index + 1}",
                text=(
                    "1. 결론\n"
                    f"{'요컨대' if index < 90 else '한편'}, 반환 약정은 증명되지 않았습니다.\n\n"
                    "2. 판단\n송금 사실만으로는 부족합니다(을 제1호증)."
                ),
            )
            for index in range(100)
        ]
        profile = build_profile("lawyer_1", briefs, version=1)
        self.assertEqual(profile.operational_rules["preferred_transition"], "요컨대")
        self.assertEqual(profile.operational_rules["sample_size"], 100)

    def test_style_exemplar_does_not_split_headings_or_dates(self) -> None:
        brief = PriorBrief(
            brief_id="brief_date",
            lawyer_id="lawyer_1",
            title="날짜가 있는 준비서면",
            text=(
                "1. 사실관계\n원고는 2025. 1. 3. 피고에게 금원을 송금하였습니다.\n\n"
                "2. 결론\n따라서 원고의 청구는 기각되어야 합니다."
            ),
        )
        profile = build_profile("lawyer_1", [brief], version=1)
        style = next(item for item in profile.traits if item.category == "style")
        self.assertNotEqual(style.evidence[0].excerpt, "1.")
        self.assertIn("2025. 1. 3.", style.evidence[0].excerpt)

    def test_profile_mean_is_computed_once(self) -> None:
        brief = PriorBrief(
            brief_id="brief_many",
            lawyer_id="lawyer_1",
            title="긴 준비서면",
            text="\n".join(
                f"피고는 제{i} 사정에 관하여 구체적으로 다투고 있습니다."
                for i in range(500)
            ),
        )
        with patch(
            "seomyeon.services.profile.mean", wraps=statistics_mean
        ) as wrapped_mean:
            build_profile("lawyer_1", [brief], version=1)
        self.assertEqual(wrapped_mean.call_count, 1)

    def test_profile_traits_are_evidence_backed(self) -> None:
        briefs = [
            PriorBrief(
                brief_id="brief_1",
                lawyer_id="lawyer_1",
                title="준비서면 1",
                text=(
                    "1. 결론\n이 사건 청구는 이유 없습니다.\n\n"
                    "2. 사실관계\n피고는 계약서를 작성하지 않았습니다(을 제1호증).\n\n"
                    "따라서 원고의 청구는 기각되어야 합니다."
                ),
            )
        ]
        profile = build_profile("lawyer_1", briefs, version=1)
        self.assertTrue(profile.traits)
        self.assertTrue(all(trait.evidence for trait in profile.traits))
        self.assertTrue(all(0 <= trait.confidence <= 1 for trait in profile.traits))
        self.assertFalse(
            any(
                trait.category == "reasoning" and "성격" in trait.statement
                for trait in profile.traits
            )
        )

    def test_profile_requires_explicit_approval_except_guardrail(self) -> None:
        brief = PriorBrief(
            brief_id="brief_1",
            lawyer_id="lawyer_1",
            title="준비서면",
            text="1. 결론\n따라서 이 사건 청구는 기각되어야 합니다.",
        )
        profile = build_profile("lawyer_1", [brief], version=1)
        candidate = next(trait for trait in profile.traits if not trait.approved)
        approved = approve_traits(profile, {candidate.trait_id})
        self.assertTrue(
            next(
                item for item in approved.traits if item.trait_id == candidate.trait_id
            ).approved
        )
        self.assertTrue(
            next(
                item for item in approved.traits if item.category == "guardrail"
            ).approved
        )

    def test_candidate_trait_requires_verbatim_evidence(self) -> None:
        brief = PriorBrief(
            brief_id="brief_1",
            lawyer_id="lawyer_1",
            title="준비서면",
            text="피고는 입증책임의 소재를 먼저 확정한 다음 개별 증거를 검토합니다.",
        )
        profile = build_profile("lawyer_1", [brief], version=1)
        updated = add_candidate_trait(
            profile,
            brief,
            "reasoning",
            "입증책임을 먼저 확정합니다.",
            0.8,
            "입증책임의 소재를 먼저 확정한 다음",
        )
        self.assertFalse(updated.traits[-1].approved)
        self.assertEqual(updated.traits[-1].evidence[0].source_id, "brief_1")
        with self.assertRaisesRegex(ValueError, "does not occur verbatim"):
            add_candidate_trait(
                profile,
                brief,
                "reasoning",
                "존재하지 않는 패턴",
                0.5,
                "원문에 존재하지 않는 충분히 긴 인용문",
            )
