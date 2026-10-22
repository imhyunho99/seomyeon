import io
import json
import unittest

from seomyeon.legal_sources import LawOpenDataClient


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class LawOpenDataClientTests(unittest.TestCase):
    def test_statute_search_and_article_import(self) -> None:
        def opener(request, *, timeout):
            if "lawSearch.do" in request.full_url:
                payload = {
                    "LawSearch": {
                        "law": [
                            {
                                "법령일련번호": "284415",
                                "법령ID": "001706",
                                "법령명한글": "민법",
                                "법령구분명": "법률",
                                "소관부처명": "법무부",
                                "시행일자": "20260317",
                            }
                        ]
                    }
                }
            else:
                payload = {
                    "법령": {
                        "기본정보": {
                            "법령명_한글": "민법",
                            "법령ID": "001706",
                            "법종구분": {"content": "법률"},
                            "소관부처": {"content": "법무부"},
                            "시행일자": "20260317",
                        },
                        "조문": {
                            "조문단위": [
                                {
                                    "조문번호": "598",
                                    "조문여부": "조문",
                                    "조문제목": "소비대차의 의의",
                                    "조문내용": "제598조(소비대차의 의의) 반환을 약정한다.",
                                }
                            ]
                        },
                    }
                }
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        client = LawOpenDataClient("private-oc", opener=opener)
        result = client.search_statutes("민법")[0]
        self.assertEqual(result.mst, "284415")
        self.assertNotIn("private-oc", result.source_url)
        article = client.get_statute_article("284415", "제598조")
        self.assertEqual(article.article_number, "598")
        self.assertIn("반환을 약정", article.article_text)
        self.assertEqual(len(article.content_hash), 64)

    def test_core_holding_keeps_majority_and_cuts_at_sentence(self) -> None:
        from seomyeon.legal_sources import core_holding

        holding = (
            "[다수의견] 중도상환수수료는 간주이자가 아니다. 구체적인 이유는 다음과 같다. "
            + "① 이유 문장입니다. " * 200
            + "[대법관 갑, 대법관 을의 반대의견] 간주이자에 해당한다."
        )
        core = core_holding(holding, max_chars=120)
        self.assertNotIn("반대의견", core)
        self.assertNotIn("[다수의견]", core)
        self.assertTrue(core.startswith("중도상환수수료는 간주이자가 아니다."))
        self.assertLessEqual(len(core), 121)
        self.assertTrue(core.endswith(".") or core.endswith("…"))

    def test_core_holding_numbered_points_are_split(self) -> None:
        from seomyeon.legal_sources import core_holding

        holding = "[1] 소비대차는 반환 약정이 있어야 성립한다. [2] 송금 사실만으로는 대여 사실이 증명되지 않는다."
        self.assertEqual(core_holding(holding), holding)

    def test_exact_statute_name_survives_small_display(self) -> None:
        # 실제 API: '민법' 검색 결과가 이름순이라 '난민법…'이 앞에 오고,
        # display=3이면 서버가 '민법'을 잘라 버리던 문제
        names = ["난민법", "난민법 시행령", "난민법 시행규칙", "민법", "민법 시행령"]
        seen = {}

        def opener(request, *, timeout):
            seen["url"] = request.full_url
            items = [
                {"법령일련번호": str(100 + i), "법령ID": str(i), "법령명한글": name,
                 "법령구분명": "법률", "소관부처명": "법무부", "시행일자": "20260101"}
                for i, name in enumerate(names)
            ]
            return FakeResponse(json.dumps({"LawSearch": {"law": items}}).encode("utf-8"))

        results = LawOpenDataClient("oc", opener=opener).search_statutes("민법", display=3)
        self.assertEqual(results[0].name, "민법")
        self.assertEqual(len(results), 3)
        # '상법'은 실제로 이름순 56건 중 34번째라 20건으로는 잘린다
        self.assertIn("display=100", seen["url"])

    def test_search_and_detail_normalize_official_response(self) -> None:
        calls = []

        def opener(request, *, timeout):
            calls.append((request.full_url, timeout))
            if "lawSearch.do" in request.full_url:
                payload = {
                    "PrecSearch": {
                        "prec": {
                            "판례일련번호": "242003",
                            "사건명": "약정금",
                            "사건번호": "2023다272289",
                            "선고일자": "2024.11.14",
                            "법원명": "대법원",
                            "사건종류명": "민사",
                            "판결유형": "판결",
                            "데이터출처명": "대법원",
                        }
                    }
                }
            else:
                payload = {
                    "PrecService": {
                        "판례정보일련번호": "242003",
                        "사건명": "약정금",
                        "사건번호": "2023다272289",
                        "선고일자": "20241114",
                        "법원명": "대법원",
                        "사건종류명": "민사",
                        "판결유형": "판결",
                        "판시사항": "[1] 쟁점<br/>[2] 추가 쟁점",
                        "판결요지": "<b>원금 반환</b>과 추가 금전 지급 약정",
                        "참조조문": "이자제한법",
                        "참조판례": "대법원 2020다1",
                        "판례내용": "전문",
                    }
                }
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        client = LawOpenDataClient("test-key", opener=opener)
        results = client.search_precedents("소비대차")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].precedent_id, "242003")
        self.assertNotIn("test-key", results[0].source_url)
        detail = client.get_precedent("242003")
        self.assertEqual(detail.holding, "원금 반환과 추가 금전 지급 약정")
        self.assertEqual(detail.issues, "[1] 쟁점\n[2] 추가 쟁점")
        self.assertEqual(len(detail.content_hash), 64)
        self.assertEqual(len(calls), 2)

    def test_precedents_by_statute_use_jo_filter(self) -> None:
        from urllib.parse import parse_qs, urlparse

        seen = {}

        def opener(request, *, timeout):
            seen.update({k: v[0] for k, v in parse_qs(urlparse(request.full_url).query).items()})
            items = [
                {"판례일련번호": "614311", "사건명": "대여금·부당이득금", "사건번호": "2024다314845",
                 "선고일자": "2025.07.03", "법원명": "대법원", "데이터출처명": "대법원"}
            ]
            return FakeResponse(json.dumps({"PrecSearch": {"prec": items}}).encode("utf-8"))

        client = LawOpenDataClient("oc", opener=opener)
        results = client.search_precedents_by_statute("민법", "598", display=50)
        self.assertEqual(results[0].case_number, "2024다314845")
        self.assertEqual(seen["JO"], "민법 제598조")
        self.assertEqual(seen["org"], "400201")
        self.assertEqual(seen["display"], "50")
        self.assertNotIn("query", seen)
        client.search_precedents_by_statute("민법", "20의2")
        self.assertEqual(seen["JO"], "민법 제20조의2")

    def test_ai_article_search_parses_branch_numbers(self) -> None:
        def opener(request, *, timeout):
            payload = {"aiSearch": {"법령조문": [
                {"조문번호": "0020", "조문가지번호": "02", "법령명": "소송촉진 등에 관한 특례법",
                 "법령ID": "001215", "법령일련번호": "286417", "조문내용": "제20조의2(공시송달) 내용"},
                {"조문번호": "0598", "조문가지번호": "00", "법령명": "민법",
                 "법령ID": "001706", "법령일련번호": "284415", "조문내용": "제598조(소비대차의 의의) 내용"},
            ]}}
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        hits = LawOpenDataClient("oc", opener=opener).ai_search_articles("빌려준 돈")
        self.assertEqual([(h.law_name, h.article_number) for h in hits],
                         [("소송촉진 등에 관한 특례법", "20의2"), ("민법", "598")])
        self.assertEqual(hits[1].mst, "284415")

    def test_manual_precedent_search_tries_case_name_then_body(self) -> None:
        from urllib.parse import parse_qs, urlparse

        calls = []

        def opener(request, *, timeout):
            params = {k: v[0] for k, v in parse_qs(urlparse(request.full_url).query).items()}
            calls.append(params)
            items = [] if params["search"] == "1" else [
                {"판례일련번호": "1", "사건명": "대여금", "사건번호": "2020다1", "선고일자": "2020.01.01",
                 "법원명": "대법원", "데이터출처명": "대법원"}
            ]
            return FakeResponse(json.dumps({"PrecSearch": {"prec": items}}).encode("utf-8"))

        results = LawOpenDataClient("oc", opener=opener).search_precedents("대여금 반환 약정")
        self.assertEqual(len(results), 1)
        self.assertEqual([c["search"] for c in calls], ["1", "2"])
        self.assertTrue(all(c["org"] == "400201" for c in calls))

    def test_branch_article_is_matched_and_fetched_alone(self) -> None:
        from urllib.parse import parse_qs, urlparse

        seen = {}

        def opener(request, *, timeout):
            seen.update({k: v[0] for k, v in parse_qs(urlparse(request.full_url).query).items()})
            payload = {"법령": {
                "기본정보": {"법령명_한글": "민법", "법령ID": "001706", "시행일자": "20260317"},
                "조문": {"조문단위": [
                    {"조문번호": "14", "조문여부": "전문", "조문내용": "제2절 능력"},
                    {"조문번호": "14", "조문가지번호": "", "조문여부": "조문", "조문제목": "성년후견종료의 심판", "조문내용": "제14조"},
                    {"조문번호": "14", "조문가지번호": "2", "조문여부": "조문", "조문제목": "특정후견의 심판", "조문내용": "제14조의2"},
                ]},
            }}
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        client = LawOpenDataClient("oc", opener=opener)
        article = client.get_statute_article("284415", "14의2")
        self.assertEqual(article.article_title, "특정후견의 심판")
        self.assertEqual(seen["JO"], "001402")
        self.assertEqual(client.get_statute_article("284415", "제14조").article_title, "성년후견종료의 심판")

    def test_article_text_includes_paragraphs_items_and_subitems(self) -> None:
        def opener(request, *, timeout):
            payload = {"법령": {
                "기본정보": {"법령명_한글": "민법", "법령ID": "001706"},
                "조문": {"조문단위": {
                    "조문번호": "14", "조문가지번호": "2", "조문여부": "조문", "조문제목": "특정후견의 심판",
                    "조문내용": "제14조의2(특정후견의 심판)",
                    "항": [
                        {"항번호": "①", "항내용": "① 가정법원은 특정후견의 심판을 한다.",
                         "호": [{"호번호": "1.", "호내용": "1. 첫째 사유", "목": {"목내용": "가. 세부"}}]},
                        {"항번호": "②", "항내용": "② 본인의 의사에 반하여 할 수 없다."},
                    ],
                }},
            }}
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        text = LawOpenDataClient("oc", opener=opener).get_statute_article("284415", "14의2").article_text
        self.assertEqual(
            text,
            "제14조의2(특정후견의 심판)\n① 가정법원은 특정후견의 심판을 한다.\n1. 첫째 사유\n가. 세부\n② 본인의 의사에 반하여 할 수 없다.",
        )

    def test_lookup_by_case_numbers_and_tax_items_are_excluded(self) -> None:
        from urllib.parse import parse_qs, urlparse

        seen = {}

        def opener(request, *, timeout):
            seen.update({k: v[0] for k, v in parse_qs(urlparse(request.full_url).query).items()})
            items = [
                {"판례일련번호": "1", "사건명": "대여금", "사건번호": "2014다26187", "선고일자": "2014.07.10",
                 "법원명": "대법원", "데이터출처명": "대법원"},
                {"판례일련번호": "2", "사건명": "국세", "사건번호": "대법원-2018-두-1", "선고일자": "2018.01.01",
                 "법원명": "", "데이터출처명": "국세법령정보시스템"},
            ]
            return FakeResponse(json.dumps({"PrecSearch": {"prec": items}}).encode("utf-8"))

        client = LawOpenDataClient("oc", opener=opener)
        results = client.search_precedents_by_case_numbers(["2014다26187", "2010다1"])
        self.assertEqual([item.case_number for item in results], ["2014다26187"])
        self.assertEqual(seen["nb"], "2014다26187,2010다1")
        self.assertEqual(seen["datSrcNm"], "대법원")
        self.assertEqual(len(client.search_precedents("대여금")), 1)

    def test_cited_case_numbers_are_parsed(self) -> None:
        from seomyeon.legal_sources import cited_case_numbers

        text = "대법원 2014. 7. 10. 선고 2014다26187 판결, 대법원 1972. 12. 12. 선고 72다221 판결(공1973, 7164)"
        self.assertEqual(cited_case_numbers(text), ["2014다26187", "72다221"])

    def test_query_and_identifier_validation(self) -> None:
        client = LawOpenDataClient("test", opener=lambda *_args, **_kwargs: None)
        with self.assertRaises(ValueError):
            client.search_precedents("한")
        with self.assertRaises(ValueError):
            client.get_precedent("../../secret")
        with self.assertRaises(ValueError):
            client.get_statute_article("../../secret", "598")


if __name__ == "__main__":
    unittest.main()
