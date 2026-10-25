import json
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from seomyeon.auth import AccountStore
from seomyeon.legal_sources import (
    PrecedentDetail,
    PrecedentSummary,
    StatuteArticle,
    StatuteSummary,
)
from seomyeon.storage import SqliteStore
from seomyeon.web import WebConfig, _validate_runtime_config, create_app


class WebWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database_path = Path(self.directory.name) / "web.sqlite3"
        self.store = SqliteStore(database_path)
        self.accounts = AccountStore(database_path)
        self.accounts.create_account(
            "pilot@example.com",
            "correct horse battery staple",
            "파일럿 변호사",
        )
        self.client = TestClient(
            create_app(
                self.store,
                WebConfig(max_request_bytes=2048, max_briefs_per_user=1),
                self.accounts,
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.directory.cleanup()

    def _login(self) -> str:
        response = self.client.post(
            "/api/login",
            json={
                "email": "pilot@example.com",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def test_auth_and_csrf_boundaries(self) -> None:
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("서면", page.text)
        self.assertEqual(self.client.get("/assets/app.css").status_code, 200)
        self.assertEqual(self.client.get("/assets/missing.css").status_code, 404)
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)
        malformed = self.client.post(
            "/api/login",
            content=b"[]",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(malformed.status_code, 400)
        oversized = self.client.post(
            "/api/login",
            content=b"x" * 2049,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(oversized.status_code, 413)
        chunked_oversized = self.client.post(
            "/api/login",
            content=(chunk for chunk in (b"x" * 1200, b"y" * 1200)),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(chunked_oversized.status_code, 413)
        token = self._login()
        old_cookie = self.client.cookies.get("seomyeon_session")
        self.assertEqual(
            self.client.post("/api/matters", json={"title": "사건"}).status_code, 403
        )
        response = self.client.post(
            "/api/matters",
            json={"title": "사건"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 201)
        invalid_boolean = self.client.post(
            f"/api/matters/{response.json()['matter_id']}/approve",
            json={
                "assertion_ids": [],
                "authority_ids": [],
                "approve_perspective": "false",
            },
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(invalid_boolean.status_code, 400)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("camera=()", response.headers["permissions-policy"])
        self.assertEqual(
            self.client.post(
                "/api/logout", headers={"X-CSRF-Token": token}
            ).status_code,
            200,
        )
        self.client.cookies.set("seomyeon_session", old_cookie)
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)

    def test_signup_and_personal_data_isolation(self) -> None:
        first_token = self._login()
        matter = self.client.post(
            "/api/matters",
            json={"title": "첫 사용자 사건"},
            headers={"X-CSRF-Token": first_token},
        ).json()
        self.client.post("/api/logout", headers={"X-CSRF-Token": first_token})

        weak_signup = self.client.post(
            "/api/signup",
            json={
                "display_name": "두 번째 변호사",
                "email": "second@example.com",
                "password": "short",
            },
        )
        self.assertEqual(weak_signup.status_code, 400)

        signup = self.client.post(
            "/api/signup",
            json={
                "display_name": "두 번째 변호사",
                "email": "second@example.com",
                "password": "second-password",
            },
        )
        self.assertEqual(signup.status_code, 201)
        duplicate = self.client.post(
            "/api/signup",
            json={
                "display_name": "중복 계정",
                "email": "second@example.com",
                "password": "another-password",
            },
        )
        self.assertEqual(duplicate.status_code, 409)
        second_token = signup.json()["csrf_token"]
        workspace = self.client.get("/api/workspace")
        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(workspace.json()["matters"], [])
        foreign_write = self.client.post(
            f"/api/matters/{matter['matter_id']}/assertions",
            json={
                "assertion_id": "F1",
                "kind": "fact",
                "text": "다른 사용자 자료",
                "source_label": "기록 1쪽",
            },
            headers={"X-CSRF-Token": second_token},
        )
        self.assertEqual(foreign_write.status_code, 404)

    def test_complete_web_drafting_flow(self) -> None:
        token = self._login()
        headers = {"X-CSRF-Token": token}
        brief = self.client.post(
            "/api/briefs",
            json={
                "title": "기존 준비서면",
                "text": (
                    "1. 결론\n따라서 이 사건 청구는 기각되어야 합니다.\n\n"
                    "2. 사실관계\n피고는 계약서를 작성하지 않았습니다(을 제1호증)."
                ),
            },
            headers=headers,
        )
        self.assertEqual(brief.status_code, 201)
        quota_response = self.client.post(
            "/api/briefs",
            json={"title": "두 번째 서면", "text": "저장 한도 확인"},
            headers=headers,
        )
        self.assertEqual(quota_response.status_code, 507)
        profile = self.client.post("/api/profile/build", headers=headers)
        self.assertEqual(profile.status_code, 201)
        trait_ids = [item["trait_id"] for item in profile.json()["traits"]]
        approved_profile = self.client.post(
            "/api/profile/approve",
            json={"trait_ids": trait_ids},
            headers=headers,
        )
        self.assertEqual(approved_profile.status_code, 200)

        matter = self.client.post(
            "/api/matters", json={"title": "대여금 사건"}, headers=headers
        ).json()
        matter_id = matter["matter_id"]
        assertion = self.client.post(
            f"/api/matters/{matter_id}/assertions",
            json={
                "assertion_id": "F1",
                "kind": "fact",
                "text": "원고가 피고에게 금원을 송금하였다.",
                "source_label": "을 제1호증",
            },
            headers=headers,
        )
        self.assertEqual(assertion.status_code, 201)
        invalid_authority = self.client.post(
            f"/api/matters/{matter_id}/authorities",
            json={
                "authority_id": "A1",
                "citation": "민법 제598조",
                "proposition": "소비대차는 반환 약정이 필요하다.",
                "source_url": "javascript:alert(1)",
            },
            headers=headers,
        )
        self.assertEqual(invalid_authority.status_code, 400)
        authority = self.client.post(
            f"/api/matters/{matter_id}/authorities",
            json={
                "authority_id": "A1",
                "citation": "민법 제598조",
                "proposition": "소비대차는 반환 약정이 필요하다.",
                "source_url": "https://law.go.kr",
            },
            headers=headers,
        )
        self.assertEqual(authority.status_code, 201)
        perspective = self.client.put(
            f"/api/matters/{matter_id}/perspective",
            json={
                "thesis": "송금만으로 소비대차 합의가 증명되지 않는다.",
                "priorities": ["반환 약정"],
                "risks": ["송금 사실"],
            },
            headers=headers,
        )
        self.assertEqual(perspective.status_code, 200)
        approval = self.client.post(
            f"/api/matters/{matter_id}/approve",
            json={
                "assertion_ids": ["F1"],
                "authority_ids": ["A1"],
                "approve_perspective": True,
            },
            headers=headers,
        )
        self.assertEqual(approval.status_code, 200)
        draft = self.client.post(
            f"/api/matters/{matter_id}/draft",
            json={"title": "준비서면 초안"},
            headers=headers,
        )
        self.assertEqual(draft.status_code, 201)
        result = draft.json()
        self.assertTrue(result["deterministic_passed"])
        self.assertFalse(result["filing_ready"])
        self.assertIn("[근거: F1]", result["content"])
        self.assertIn("[법률: A1]", result["content"])
        self.assertIn("반환 약정의 순서로", result["content"])
        self.assertIn("송금 사실은 반대 방향의 판단 요소", result["content"])
        self.assertTrue(result["applied_trait_ids"])
        workspace = self.client.get("/api/workspace")
        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(workspace.json()["drafts"][0]["draft_id"], result["draft_id"])

    def test_production_configuration_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            _validate_runtime_config(WebConfig(production=True))
        with self.assertRaises(RuntimeError):
            _validate_runtime_config(
                WebConfig(
                    production=True,
                    cookie_secure=True,
                    allowed_origins=("http://legal.example",),
                )
            )
        _validate_runtime_config(
            WebConfig(
                production=True,
                cookie_secure=True,
                allowed_origins=("https://legal.example",),
                allowed_hosts=("legal.example",),
            )
        )

    def test_structured_argument_plan_and_draft_flow(self) -> None:
        self.client.close()
        self.client = TestClient(
            create_app(
                self.store,
                WebConfig(max_request_bytes=20_000, max_briefs_per_user=100),
                self.accounts,
            )
        )
        token = self._login()
        headers = {"X-CSRF-Token": token}
        for index in range(100):
            brief = self.client.post(
                "/api/briefs",
                json={
                    "title": f"기존 준비서면 {index + 1}",
                    "text": (
                        f"1. 결론\n요컨대 원고의 제{index + 1} 청구는 "
                        "기각되어야 합니다.\n\n"
                        "2. 판단\n반환 약정은 증명되지 않았습니다"
                        f"(을 제{index % 9 + 1}호증)."
                    ),
                },
                headers=headers,
            )
            self.assertEqual(brief.status_code, 201)
        profile = self.client.post("/api/profile/build", headers=headers).json()
        self.assertEqual(profile["operational_rules"]["sample_size"], 100)
        self.assertEqual(profile["operational_rules"]["preferred_transition"], "요컨대")
        approved = self.client.post(
            "/api/profile/approve",
            json={"trait_ids": [item["trait_id"] for item in profile["traits"]]},
            headers=headers,
        )
        self.assertEqual(approved.status_code, 200)
        matter_id = self.client.post(
            "/api/matters", json={"title": "대여금 사건"}, headers=headers
        ).json()["matter_id"]
        for payload in (
            {
                "assertion_id": "F1",
                "kind": "fact",
                "text": "원고가 피고에게 3천만 원을 송금하였다.",
                "source_label": "갑 제1호증 2쪽",
            },
            {
                "assertion_id": "F2",
                "kind": "disputed_fact",
                "text": "피고는 송금액이 투자금이라고 다투고 있다.",
                "source_label": "답변서 3쪽",
            },
        ):
            response = self.client.post(
                f"/api/matters/{matter_id}/assertions",
                json=payload,
                headers=headers,
            )
            self.assertEqual(response.status_code, 201)
        authority = self.client.post(
            f"/api/matters/{matter_id}/authorities",
            json={
                "authority_id": "A1",
                "citation": "민법 제598조",
                "proposition": "소비대차는 금전 이전과 반환 약정으로 성립한다.",
                "source_url": "https://law.go.kr",
            },
            headers=headers,
        )
        self.assertEqual(authority.status_code, 201)
        perspective = self.client.put(
            f"/api/matters/{matter_id}/perspective",
            json={
                "thesis": "송금만으로 반환 약정이 증명되지 않는다.",
                "priorities": ["반환 약정"],
                "risks": ["송금 사실"],
            },
            headers=headers,
        )
        self.assertEqual(perspective.status_code, 200)
        approval = self.client.post(
            f"/api/matters/{matter_id}/approve",
            json={
                "assertion_ids": ["F1", "F2"],
                "authority_ids": ["A1"],
                "approve_perspective": True,
            },
            headers=headers,
        )
        self.assertEqual(approval.status_code, 200)
        structure = self.client.put(
            f"/api/matters/{matter_id}/case-structure",
            json={
                "case_metadata": {
                    "court": "서울중앙지방법원 귀중",
                    "case_number": "2026가단12345",
                    "document_type": "준비서면",
                    "filing_party": "피고 홍길동",
                    "opposing_party": "원고 김철수",
                    "relief_sought": "원고의 청구를 기각한다.",
                },
                "claims": [
                    {
                        "claim_id": "C1",
                        "label": "대여금 반환청구",
                        "position": "defense",
                        "requested_relief": "청구 기각",
                        "elements": [
                            {
                                "element_id": "E1",
                                "name": "반환 약정",
                                "legal_test": "소비대차는 금전 이전과 반환 약정으로 성립한다.",
                                "burden_party": "원고",
                                "conclusion": "not_satisfied",
                                "assertion_ids": ["F1"],
                                "authority_ids": ["A1"],
                            }
                        ],
                    }
                ],
                "opposing_positions": [
                    {
                        "position_id": "P1",
                        "allegation": "송금액은 대여금이다.",
                        "response": "deny",
                        "response_text": "송금 사실만으로 반환 약정은 증명되지 않는다.",
                        "assertion_ids": ["F1", "F2"],
                        "authority_ids": ["A1"],
                    }
                ],
            },
            headers=headers,
        )
        self.assertEqual(structure.status_code, 200, structure.text)
        plan = self.client.post(
            f"/api/matters/{matter_id}/argument-plan", json={}, headers=headers
        )
        self.assertEqual(plan.status_code, 201)
        self.assertEqual(plan.json()["unresolved"], [])
        plan_approval = self.client.post(
            f"/api/matters/{matter_id}/argument-plan/approve",
            json={"approve": True},
            headers=headers,
        )
        self.assertEqual(plan_approval.status_code, 200)
        draft = self.client.post(
            f"/api/matters/{matter_id}/draft",
            json={"title": "구조화 준비서면"},
            headers=headers,
        )
        self.assertEqual(draft.status_code, 201, draft.text)
        result = draft.json()
        self.assertTrue(result["deterministic_passed"])
        self.assertIn("서울중앙지방법원 귀중", result["content"])
        self.assertIn("입증책임", result["content"])
        self.assertIn("상대방 주장에 대한 부인", result["content"])
        self.assertIn("판단 범위에서만 적용한다", result["content"])
        self.assertIn("[근거: F1]", result["content"])
        self.assertIn("[법률: A1]", result["content"])

    def test_official_precedent_search_and_import(self) -> None:
        class FakeLawClient:
            def search_precedents(self, query, *, display=10):
                self.query = query
                self.display = display
                return [
                    PrecedentSummary(
                        "242003",
                        "약정금",
                        "2023다272289",
                        "2024.11.14",
                        "대법원",
                        "민사",
                        "판결",
                        "대법원",
                        "https://www.law.go.kr/LSW/precInfoP.do?precSeq=242003",
                    )
                ]

            def get_precedent(self, precedent_id):
                return PrecedentDetail(
                    precedent_id,
                    "약정금",
                    "2023다272289",
                    "20241114",
                    "대법원",
                    "민사",
                    "판결",
                    "대법원",
                    "https://www.law.go.kr/LSW/precInfoP.do?precSeq=242003",
                    "이자제한법 적용 여부",
                    "원금 반환과 추가 금전 지급을 확정적으로 보장한 약정에 관한 법리",
                    "이자제한법",
                    "",
                    "2026-09-20T00:00:00+00:00",
                    "a" * 64,
                )

            def search_statutes(self, query, *, display=10):
                return [
                    StatuteSummary(
                        "284415",
                        "001691",
                        "민법",
                        "법률",
                        "법무부",
                        "2026. 1. 1.",
                        "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=284415",
                    )
                ]

            def get_statute_article(self, mst, article_number):
                return StatuteArticle(
                    "284415",
                    "001691",
                    "민법",
                    "법률",
                    "법무부",
                    "2026. 1. 1.",
                    "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=284415",
                    "598",
                    "소비대차의 의의",
                    "당사자 한쪽이 금전을 이전하고 반환할 것을 약정함으로써 효력이 생긴다.",
                    "2026-09-20T00:00:00+00:00",
                    "b" * 64,
                )

        self.client.close()
        law_client = FakeLawClient()
        self.client = TestClient(
            create_app(
                self.store,
                WebConfig(max_request_bytes=2048),
                self.accounts,
                law_client,
            )
        )
        token = self._login()
        headers = {"X-CSRF-Token": token}
        matter = self.client.post(
            "/api/matters", json={"title": "공식 판례 사건"}, headers=headers
        ).json()
        search = self.client.get("/api/legal/precedents", params={"query": "소비대차"})
        self.assertEqual(search.status_code, 200)
        self.assertTrue(search.json()["official"])
        self.assertEqual(search.json()["results"][0]["precedent_id"], "242003")
        self.assertEqual(law_client.display, 5)
        imported = self.client.post(
            f"/api/matters/{matter['matter_id']}/authorities/import-precedent",
            json={"authority_id": "A1", "precedent_id": "242003"},
            headers=headers,
        )
        self.assertEqual(imported.status_code, 201)
        authority = imported.json()
        self.assertEqual(authority["official_source"], "국가법령정보센터")
        self.assertEqual(authority["official_id"], "242003")
        self.assertFalse(authority["verified"])
        self.assertNotIn("test", authority["source_url"])
        statute_search = self.client.get(
            "/api/legal/statutes", params={"query": "민법"}
        )
        self.assertEqual(statute_search.status_code, 200)
        statute = self.client.post(
            f"/api/matters/{matter['matter_id']}/authorities/import-statute",
            json={"authority_id": "A2", "mst": "284415", "article_number": "598"},
            headers=headers,
        )
        self.assertEqual(statute.status_code, 201)
        self.assertEqual(statute.json()["official_id"], "law:284415:598")
        for _ in range(28):
            self.assertEqual(
                self.client.get(
                    "/api/legal/precedents", params={"query": "소비대차"}
                ).status_code,
                200,
            )
        limited = self.client.get("/api/legal/precedents", params={"query": "소비대차"})
        self.assertEqual(limited.status_code, 429)


if __name__ == "__main__":
    unittest.main()


class BriefUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database_path = Path(self.directory.name) / "web.sqlite3"
        self.accounts = AccountStore(database_path)
        self.accounts.create_account("pilot@example.com", "correct horse battery", "파일럿")
        self.client = TestClient(
            create_app(
                SqliteStore(database_path),
                WebConfig(max_request_bytes=2048, max_upload_bytes=64_000),
                self.accounts,
            )
        )
        token = self.client.post(
            "/api/login",
            json={"email": "pilot@example.com", "password": "correct horse battery"},
        ).json()["csrf_token"]
        self.headers = {"X-CSRF-Token": token}

    def tearDown(self) -> None:
        self.client.close()
        self.directory.cleanup()

    def _upload(self, filename: str, data: bytes, **extra):
        from urllib.parse import quote

        return self.client.post(
            "/api/briefs/upload",
            content=data,
            headers={
                **self.headers,
                "Content-Type": "application/octet-stream",
                "X-Filename": quote(filename),
                **extra,
            },
        )

    def _hwpx(self, text: str) -> bytes:
        import io
        import zipfile

        body = "".join(
            f"<hp:p><hp:run><hp:t>{line}</hp:t></hp:run></hp:p>"
            for line in text.split("\n")
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "Contents/section0.xml",
                f'<hs:sec xmlns:hp="urn:hp" xmlns:hs="urn:hs">{body}</hs:sec>',
            )
        return buffer.getvalue()

    def test_hwpx_upload_is_extracted_and_pseudonymized(self) -> None:
        # 요청 본문 한도(2048)보다 큰 파일도 업로드 경로에서는 받는다
        text = "피고인 박영수\n" + "박영수는 공소사실을 부인합니다.\n" * 120
        response = self._upload("260313_형사_박영수_항소이유서.hwpx", self._hwpx(text))
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["title"], "260313_형사_A_항소이유서")
        self.assertNotIn("박영수", body["text"])
        self.assertEqual(body["redaction"]["names"], 1)
        self.assertNotIn("박영수", json.dumps(body, ensure_ascii=False))

    def test_upload_requires_csrf(self) -> None:
        response = self.client.post(
            "/api/briefs/upload",
            content=b"x",
            headers={"X-Filename": "a.txt"},
        )
        self.assertEqual(response.status_code, 403)

    def test_upload_limit_and_format_errors(self) -> None:
        self.assertEqual(self._upload("a.hwp", b"0" * 64_001).status_code, 413)
        self.assertEqual(self._upload("a.pdf", b"%PDF").status_code, 400)
        self.assertEqual(self._upload("a.hwp", b"broken").status_code, 400)

    def test_text_brief_is_pseudonymized_by_default(self) -> None:
        response = self.client.post(
            "/api/briefs",
            json={"title": "준비서면", "text": "원고 홍길동\n홍길동은 송금하였습니다."},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("홍길동", response.json()["text"])


class ModelDraftWebTests(unittest.TestCase):
    def _run_flow(self, backend):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database_path = Path(directory.name) / "web.sqlite3"
        accounts = AccountStore(database_path)
        accounts.create_account("pilot@example.com", "correct horse battery", "파일럿")
        client = TestClient(
            create_app(SqliteStore(database_path), WebConfig(), accounts, model_backend=backend)
        )
        self.addCleanup(client.close)
        token = client.post(
            "/api/login",
            json={"email": "pilot@example.com", "password": "correct horse battery"},
        ).json()["csrf_token"]
        h = {"X-CSRF-Token": token}
        client.post("/api/briefs", json={"title": "서면", "text": "1. 결론\n따라서 기각되어야 합니다."}, headers=h)
        client.post("/api/profile/build", headers=h)
        matter_id = client.post("/api/matters", json={"title": "대여금"}, headers=h).json()["matter_id"]
        client.post(
            f"/api/matters/{matter_id}/assertions",
            json={"assertion_id": "F1", "kind": "fact", "text": "원고가 피고에게 금원을 송금하였다.", "source_label": "을 제1호증"},
            headers=h,
        )
        client.post(
            f"/api/matters/{matter_id}/authorities",
            json={"authority_id": "A1", "citation": "민법 제598조", "proposition": "소비대차는 반환 약정이 필요하다.", "source_url": "https://law.go.kr"},
            headers=h,
        )
        client.put(
            f"/api/matters/{matter_id}/perspective",
            json={"thesis": "송금만으로 소비대차 합의가 증명되지 않는다.", "priorities": [], "risks": []},
            headers=h,
        )
        client.post(
            f"/api/matters/{matter_id}/approve",
            json={"assertion_ids": ["F1"], "authority_ids": ["A1"], "approve_perspective": True},
            headers=h,
        )
        response = client.post(f"/api/matters/{matter_id}/draft", json={}, headers=h)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_model_refines_and_semantic_check_runs(self) -> None:
        from seomyeon.llm import FakeBackend

        def respond(system, user, schema):
            if "verdict" in schema["properties"]:
                return {"verdict": "partial", "reason": "반환 약정 부분은 근거 없음"}
            paragraph = user.split("<paragraph>\n", 1)[1].rsplit("\n</paragraph>", 1)[0]
            return {"text": paragraph.replace("따르면", "의하면")}

        result = self._run_flow(FakeBackend(respond))
        self.assertEqual(result["generation"]["model"], "fake")
        self.assertGreater(result["generation"]["rewritten_paragraphs"], 0)
        self.assertIn("의하면", result["content"])
        self.assertIn("semantic_partial", {item["code"] for item in result["findings"]})

    def test_without_model_draft_is_deterministic(self) -> None:
        result = self._run_flow(None)
        self.assertEqual(result["generation"]["model"], "deterministic")


class AuthoritySuggestWebTests(unittest.TestCase):
    def _client(self, law_client):
        from tests.test_authority_finder import FakeLaw  # noqa: F401  (shared fake)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database_path = Path(directory.name) / "web.sqlite3"
        accounts = AccountStore(database_path)
        accounts.create_account("pilot@example.com", "correct horse battery", "파일럿")
        client = TestClient(create_app(SqliteStore(database_path), WebConfig(), accounts, law_client))
        self.addCleanup(client.close)
        token = client.post(
            "/api/login", json={"email": "pilot@example.com", "password": "correct horse battery"}
        ).json()["csrf_token"]
        h = {"X-CSRF-Token": token}
        matter_id = client.post("/api/matters", json={"title": "대여금"}, headers=h).json()["matter_id"]
        client.post(
            f"/api/matters/{matter_id}/assertions",
            json={"assertion_id": "F1", "kind": "fact", "text": "원고가 피고에게 금원을 송금하였다.", "source_label": "갑 제1호증"},
            headers=h,
        )
        client.put(
            f"/api/matters/{matter_id}/perspective",
            json={"thesis": "소비대차가 성립하였다.", "priorities": ["소비대차 반환 약정"], "risks": []},
            headers=h,
        )
        return client, h, matter_id

    def test_suggest_returns_candidates_without_adding_authorities(self) -> None:
        from tests.test_authority_finder import FakeLaw

        client, h, matter_id = self._client(FakeLaw())
        response = client.post(f"/api/matters/{matter_id}/authorities/suggest", json={}, headers=h)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["precedents"])
        self.assertIn("notes", body)
        matter = client.get("/api/workspace").json()["matters"][0]
        self.assertEqual(matter["authorities"], [])

    def test_suggest_requires_law_client(self) -> None:
        client, h, matter_id = self._client(None)
        response = client.post(f"/api/matters/{matter_id}/authorities/suggest", json={}, headers=h)
        self.assertEqual(response.status_code, 503)


class SignupToggleTests(unittest.TestCase):
    def test_signup_can_be_disabled_for_invite_only_pilots(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database_path = Path(directory.name) / "web.sqlite3"
        client = TestClient(
            create_app(SqliteStore(database_path), WebConfig(allow_signup=False), AccountStore(database_path))
        )
        self.addCleanup(client.close)
        response = client.post(
            "/api/signup",
            json={"email": "stranger@example.com", "password": "long enough password", "display_name": "x"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("초대", response.json()["error"])
        self.assertFalse(client.get("/api/session").json().get("signup_enabled", True))

    def test_signup_flag_from_env(self) -> None:
        from unittest.mock import patch

        from seomyeon.web import _config_from_env

        with patch.dict("os.environ", {"SEOMYEON_ALLOW_SIGNUP": "0"}, clear=True):
            self.assertFalse(_config_from_env().allow_signup)
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(_config_from_env().allow_signup)
