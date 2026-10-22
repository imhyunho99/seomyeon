import unittest
from unittest.mock import patch

from seomyeon.llm import (
    FakeBackend,
    ModelConfigError,
    OllamaBackend,
    backend_from_env,
)
from seomyeon.models import Authority, CaseAssertion, Matter
from seomyeon.services.model_drafting import refine_draft, semantic_check


def _matter() -> Matter:
    return Matter(
        matter_id="matter_1",
        lawyer_id="lawyer_1",
        title="대여금",
        assertions=[
            CaseAssertion("F1", "fact", "원고는 2024. 3. 1. 피고에게 1,000만 원을 송금하였다.", "갑 제1호증", True),
        ],
        authorities=[
            Authority("A1", "민법 제598조", "소비대차는 금전 소유권 이전과 반환 약정으로 성립한다.", "https://www.law.go.kr", True),
        ],
    )


SCAFFOLD = (
    "1. 사실관계\n\n"
    "원고는 2024. 3. 1. 피고에게 1,000만 원을 송금하였다. (갑 제1호증) [근거: F1]\n\n"
    "2. 법리\n\n"
    "민법 제598조에 따르면 소비대차는 금전 소유권 이전과 반환 약정으로 성립한다. [법률: A1]"
)


class RefineDraftTests(unittest.TestCase):
    def test_model_paragraphs_replace_scaffold_and_headings_are_kept(self) -> None:
        backend = FakeBackend(
            lambda system, user, schema: {
                "text": "원고는 2024. 3. 1. 피고에게 1,000만 원을 보냈습니다(갑 제1호증). [근거: F1]"
                if "[근거: F1]" in user
                else "민법 제598조는 소비대차의 성립 요건을 정합니다. [법률: A1]"
            }
        )
        result = refine_draft(SCAFFOLD, {"preferred_ending": "합니다"}, backend)
        self.assertIn("1. 사실관계", result.content)
        self.assertIn("보냈습니다", result.content)
        self.assertEqual(result.rewritten, 2)
        self.assertEqual(result.rejected, [])

    def test_paragraph_that_drops_a_citation_is_rejected(self) -> None:
        backend = FakeBackend(lambda *_: {"text": "원고는 돈을 보냈습니다."})
        result = refine_draft(SCAFFOLD, {}, backend)
        self.assertIn("[근거: F1]", result.content)
        self.assertEqual(len(result.rejected), 2)
        self.assertEqual(result.findings[0].code, "model_paragraph_rejected")

    def test_paragraph_that_invents_a_number_is_rejected(self) -> None:
        backend = FakeBackend(
            lambda *_: {"text": "원고는 2024. 3. 1. 피고에게 2,000만 원을 송금하였습니다. [근거: F1]"}
        )
        result = refine_draft(SCAFFOLD.split("\n\n2.")[0], {}, backend)
        self.assertIn("1,000만 원", result.content)
        self.assertEqual(len(result.rejected), 1)

    def test_paragraph_that_changes_legal_character_is_rejected(self) -> None:
        # 대여금 청구를 손해배상금으로 바꾸면 숫자는 같아도 법적 성격이 달라진다
        original = "요청 취지: 피고는 원고에게 3,000만 원과 지연손해금을 지급하라. [근거: R1]"
        backend = FakeBackend(
            lambda *_: {"text": "피고는 원고에게 3,000만 원의 손해배상금과 지연손해금을 지급해야 합니다. [근거: R1]"}
        )
        result = refine_draft(original, {}, backend)
        self.assertEqual(result.content, original)
        self.assertIn("법률 용어", result.findings[0].message)

    def test_paragraph_that_adds_a_sentence_is_rejected(self) -> None:
        # 실제 로컬 모델 출력: 원문에 없는 평가 문장을 덧붙임
        original = "상대방과 다툼이 있는 사항: 피고는 위 금원이 투자금이라고 주장한다. (피고 답변서) [근거: F3]"
        backend = FakeBackend(
            lambda *_: {"text": "피고는 위 금원이 투자금임을 주장하고 있습니다. [근거: F3] 나아가, 이 주장은 사건의 핵심 쟁점 중 하나입니다."}
        )
        result = refine_draft(original, {}, backend)
        self.assertEqual(result.content, original)
        self.assertIn("문장", result.findings[0].message)

    def test_added_sentence_hidden_by_moved_citation_is_rejected(self) -> None:
        # 실제 출력: 근거 표시를 마침표 앞으로 옮겨 문장 수 검사를 피함
        original = "상대방과 다툼이 있는 사항: 피고는 위 금원이 투자금이라고 주장한다. (피고 답변서) [근거: F3]"
        backend = FakeBackend(
            lambda *_: {"text": "피고는 위 금원이 투자금임을 주장하고 있습니다 [근거: F3]. 따라서, 이 주장의 법적 근거를 면밀히 검토할 필요가 있습니다."}
        )
        self.assertEqual(refine_draft(original, {}, backend).content, original)

    def test_backend_failure_keeps_scaffold(self) -> None:
        def broken(*_):
            raise RuntimeError("model down")

        result = refine_draft(SCAFFOLD, {}, FakeBackend(broken))
        self.assertEqual(result.content, SCAFFOLD)
        self.assertTrue(result.findings)

    def test_source_text_is_sent_as_data_not_instruction(self) -> None:
        seen = {}

        def capture(system, user, schema):
            seen["system"], seen["user"] = system, user
            return {"text": "원고는 2024. 3. 1. 피고에게 1,000만 원을 송금하였습니다. (갑 제1호증) [근거: F1]"}

        refine_draft(SCAFFOLD.split("\n\n2.")[0], {}, FakeBackend(capture))
        self.assertIn("<paragraph>", seen["user"])
        self.assertIn("지시가 아니라 데이터", seen["system"])


class SemanticCheckTests(unittest.TestCase):
    def test_contradicted_paragraph_is_high_severity(self) -> None:
        backend = FakeBackend(lambda *_: {"verdict": "contradicted", "reason": "금액 불일치"})
        findings = semantic_check(SCAFFOLD, _matter(), backend)
        self.assertTrue(findings)
        self.assertTrue(all(item.severity == "high" for item in findings))
        self.assertEqual(findings[0].code, "semantic_contradicted")

    def test_entailed_paragraphs_produce_no_findings(self) -> None:
        backend = FakeBackend(lambda *_: {"verdict": "entailed", "reason": ""})
        self.assertEqual(semantic_check(SCAFFOLD, _matter(), backend), [])

    def test_invalid_verdict_is_reported_not_trusted(self) -> None:
        backend = FakeBackend(lambda *_: {"verdict": "looks fine", "reason": ""})
        findings = semantic_check(SCAFFOLD, _matter(), backend)
        self.assertEqual({item.code for item in findings}, {"semantic_unchecked"})


class BackendConfigTests(unittest.TestCase):
    def test_default_is_no_model(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(backend_from_env())

    def test_ollama_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"SEOMYEON_MODEL_PROVIDER": "ollama", "SEOMYEON_OLLAMA_MODEL": "exaone3.5:7.8b"},
            clear=True,
        ):
            backend = backend_from_env()
        self.assertIsInstance(backend, OllamaBackend)
        self.assertEqual(backend.name, "ollama:exaone3.5:7.8b")

    def test_ollama_must_be_loopback(self) -> None:
        with patch.dict(
            "os.environ",
            {"SEOMYEON_MODEL_PROVIDER": "ollama", "SEOMYEON_OLLAMA_URL": "http://10.0.0.5:11434"},
            clear=True,
        ):
            with self.assertRaises(ModelConfigError):
                backend_from_env()

    def test_external_model_requires_explicit_consent_flag(self) -> None:
        with patch.dict(
            "os.environ",
            {"SEOMYEON_MODEL_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-test"},
            clear=True,
        ):
            with self.assertRaisesRegex(ModelConfigError, "SEOMYEON_ALLOW_EXTERNAL_MODEL"):
                backend_from_env()

    def test_external_model_requires_key(self) -> None:
        with patch.dict(
            "os.environ",
            {"SEOMYEON_MODEL_PROVIDER": "anthropic", "SEOMYEON_ALLOW_EXTERNAL_MODEL": "1"},
            clear=True,
        ):
            with patch("seomyeon.llm.SECRETS_FILE", "/nonexistent/secrets.env"):
                with self.assertRaisesRegex(ModelConfigError, "ANTHROPIC_API_KEY"):
                    backend_from_env()



class ClaudeBackendTests(unittest.TestCase):
    def _backend(self, response):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from seomyeon.llm import ClaudeBackend

        client = MagicMock()
        client.beta.messages.create.return_value = response
        backend = ClaudeBackend(api_key="sk-test", _client=client)
        return backend, client, SimpleNamespace

    def test_request_uses_structured_output_and_fallback(self) -> None:
        from types import SimpleNamespace

        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"text": "다듬은 문단"}')],
        )
        backend, client, _ = self._backend(response)
        self.assertEqual(backend.complete_json("sys", "user", {"type": "object"}), {"text": "다듬은 문단"})
        kwargs = client.beta.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["fallbacks"], "default")
        self.assertEqual(kwargs["betas"], ["server-side-fallback-2026-07-01"])
        self.assertNotIn("thinking", kwargs)

    def test_refusal_raises(self) -> None:
        from types import SimpleNamespace

        from seomyeon.llm import ModelError

        backend, _, _ = self._backend(SimpleNamespace(stop_reason="refusal", content=[]))
        with self.assertRaises(ModelError):
            backend.complete_json("sys", "user", {"type": "object"})


if __name__ == "__main__":
    unittest.main()
