import unittest

from seomyeon.legal_sources import (
    ArticleHit,
    LegalSourceError,
    PrecedentDetail,
    PrecedentSummary,
    StatuteArticle,
    StatuteSummary,
)
from seomyeon.llm import FakeBackend
from seomyeon.models import CaseAssertion, Matter, Perspective
from seomyeon.services.authority_finder import find_authorities, safe_query


def _summary(pid: str, name: str) -> PrecedentSummary:
    return PrecedentSummary(pid, name, f"2020다{pid}", "2020.01.01", "대법원", "민사", "판결", "대법원",
                            f"https://www.law.go.kr/LSW/precInfoP.do?precSeq={pid}")


class FakeLaw:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.detail_calls: list[str] = []

    def search_statutes(self, query, *, display=10):
        self.queries.append(query)
        return [StatuteSummary("284415", "001706", "민법", "법률", "법무부", "20260317",
                               "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=284415")] if query == "민법" else []

    def get_statute_article(self, mst, article_number):
        if article_number == "9999":
            raise LegalSourceError("제9999조를 찾을 수 없습니다.")
        name = "소송촉진 등에 관한 특례법" if mst == "286417" else "민법"
        return StatuteArticle(mst, "001706", name, "법률", "법무부", "20260317",
                              f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={mst}", article_number,
                              "소비대차의 의의", f"제{article_number}조 소비대차는 반환을 약정함으로써 효력이 생긴다.",
                              "2026-10-18T00:00:00+00:00", "h" * 64)

    def ai_search_articles(self, text, *, display=5):
        self.queries.append(text)
        return [ArticleHit("소송촉진 등에 관한 특례법", "001215", "286417", "20의2", "제20조의2(공시송달에 의한 지급명령)")]

    def search_precedents_by_statute(self, law_name, article_number, *, display=50):
        self.queries.append(f"{law_name} {article_number}")
        return [_summary("1", "대여금"), _summary("2", "약정금"), _summary("3", "소유권이전등기")]

    def search_precedents(self, query, *, display=10, scope="auto"):
        self.queries.append(query)
        return [_summary("4", "대여금")]

    def search_precedents_by_case_numbers(self, numbers, *, display=20):
        return []

    def get_precedent(self, precedent_id):
        self.detail_calls.append(precedent_id)
        summary = _summary(precedent_id, "대여금")
        issues = "송금만으로 소비대차 합의가 증명되는지 여부" if precedent_id in {"1", "4"} else "등기 말소 청구"
        return PrecedentDetail(**summary.__dict__, issues=issues, holding="[다수의견] 송금 사실만으로는 대여의 합의가 증명되지 않는다.",
                               referenced_statutes="민법 제598조", referenced_cases="", retrieved_at="2026-10-18", content_hash="h" * 64)


def _matter() -> Matter:
    return Matter(
        matter_id="m1", lawyer_id="l1", title="대여금",
        assertions=[
            CaseAssertion("F1", "fact", "원고 홍길동은 2024. 5. 10. 피고 명의 계좌로 3,000만 원을 송금하였다.", "갑 제1호증", True),
            CaseAssertion("F3", "disputed_fact", "피고는 위 금원이 투자금이라고 주장한다.", "답변서", True),
            CaseAssertion("X9", "fact", "확인되지 않은 기록", "메모", False),
        ],
        perspective=Perspective("송금과 변제 약속으로 소비대차가 성립하였다.", ["반환 약정"], ["차용증 부존재"], True),
    )


def _model(system, user, schema):
    props = schema["properties"]
    if "issues" in props:
        return {"issues": [{
            "issue": "송금 금원이 대여금인지 투자금인지",
            "statutes": [{"law": "민법", "article": "598"}, {"law": "민법", "article": "9999"}],
            "keywords": ["대여금 홍길동", "소비대차 2024"],
        }]}
    if "relevant" in props:
        return {"relevant": "소송촉진" not in user, "reason": "쟁점과 직접 관련"}
    return {"score": 3 if "소비대차 합의" in user else 0, "reason": "판시사항이 쟁점과 같음"}


class SafeQueryTests(unittest.TestCase):
    def test_names_digits_and_aliases_are_removed(self) -> None:
        self.assertEqual(safe_query("대여금 홍길동 2024 A 반환", {"홍길동"}), "대여금 반환")

    def test_empty_after_cleaning(self) -> None:
        self.assertEqual(safe_query("홍길동 2024", {"홍길동"}), "")


class FindAuthoritiesTests(unittest.TestCase):
    def test_model_driven_search_returns_ranked_verified_candidates(self) -> None:
        law = FakeLaw()
        result = find_authorities(_matter(), law, FakeBackend(_model))
        self.assertEqual(result.issues, ["송금 금원이 대여금인지 투자금인지"])
        statutes = [(item.law_name, item.article_number) for item in result.statutes]
        # 제안 조문은 공식 원문을 가져와야 후보가 되고, 무관한 자연어 검색 결과는 걸러진다
        self.assertEqual(statutes, [("민법", "598")])
        self.assertTrue(any("9999" in note for note in result.notes))
        self.assertEqual({item.precedent_id for item in result.precedents}, {"1", "4"})
        self.assertTrue(all(item.reason for item in result.precedents))

    def test_no_name_or_number_leaves_the_machine(self) -> None:
        law = FakeLaw()
        find_authorities(_matter(), law, FakeBackend(_model))
        sent = " ".join(law.queries)
        self.assertNotIn("홍길동", sent)
        self.assertNotIn("2024", sent)
        self.assertNotIn("3,000", sent)

    def test_unconfirmed_assertions_are_not_used(self) -> None:
        backend = FakeBackend(_model)
        find_authorities(_matter(), FakeLaw(), backend)
        self.assertNotIn("확인되지 않은 기록", backend.calls[0][1])

    def test_without_model_uses_perspective_and_lexical_ranking(self) -> None:
        law = FakeLaw()
        result = find_authorities(_matter(), law, None)
        self.assertTrue(result.precedents)
        self.assertTrue(result.notes)
        self.assertNotIn("홍길동", " ".join(law.queries))

    def test_precedent_found_by_two_routes_is_fetched_first(self) -> None:
        law = FakeLaw()
        law.search_precedents = lambda query, *, display=10, scope="auto": [_summary("3", "소유권이전등기")]
        find_authorities(_matter(), law, FakeBackend(_model), max_details=1)
        self.assertEqual(law.detail_calls, ["3"])

    def test_leading_case_cited_by_candidates_is_added(self) -> None:
        law = FakeLaw()
        original = law.get_precedent

        def detail(pid):
            found = original(pid)
            if pid in {"1", "2", "3"}:
                from dataclasses import replace
                return replace(found, referenced_cases="대법원 2014. 7. 10. 선고 2014다26187 판결")
            if pid == "77":
                from dataclasses import replace
                return replace(found, case_number="2014다26187",
                               issues="송금 사실은 인정되나 대여인지 다툼이 있는 경우 소비대차 합의의 증명책임")
            return found

        law.get_precedent = detail
        law.search_precedents_by_case_numbers = lambda numbers, **_: (
            [_summary("77", "대여금")] if "2014다26187" in numbers else []
        )
        result = find_authorities(_matter(), law, FakeBackend(_model))
        leading = [item for item in result.precedents if item.precedent_id == "77"]
        self.assertEqual(len(leading), 1)
        self.assertIn("인용", leading[0].via)

    def test_detail_fetches_are_bounded(self) -> None:
        law = FakeLaw()
        find_authorities(_matter(), law, FakeBackend(_model), max_details=2)
        self.assertLessEqual(len(law.detail_calls), 2)


if __name__ == "__main__":
    unittest.main()
