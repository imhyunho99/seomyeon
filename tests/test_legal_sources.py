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
