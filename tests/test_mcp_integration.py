import tempfile
import unittest
from pathlib import Path

from mcp import Client

import seomyeon.server as server
from seomyeon.admin import approve_matter_inputs, approve_profile
from seomyeon.storage import JsonStore


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_grounded_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server.store = JsonStore(Path(directory) / "store.json")
            previous_workspace = server.WORKSPACE_ID
            server.WORKSPACE_ID = "lawyer_1"
            try:
                async with Client(server.mcp) as client:
                    await self._run_workflow(client)
            finally:
                server.WORKSPACE_ID = previous_workspace

    async def _run_workflow(self, client: Client) -> None:
        brief_result = await client.call_tool(
            "register_prior_brief",
            {
                "lawyer_id": "lawyer_1",
                "title": "준비서면 샘플",
                "text": (
                    "1. 결론\n따라서 이 사건 청구는 기각되어야 합니다.\n"
                    "2. 사실관계\n피고는 계약서를 작성하지 않았습니다(을 제1호증)."
                ),
            },
        )
        brief_id = brief_result.structured_content["brief_id"]
        profile_result = await client.call_tool(
            "build_lawyer_profile", {"lawyer_id": "lawyer_1"}
        )
        profile = profile_result.structured_content
        self.assertIsNotNone(profile)
        trait_ids = [item["trait_id"] for item in profile["traits"]]
        candidate_result = await client.call_tool(
            "add_profile_trait_candidate",
            {
                "lawyer_id": "lawyer_1",
                "brief_id": brief_id,
                "category": "reasoning",
                "statement": "핵심 결론을 먼저 제시합니다.",
                "confidence": 0.8,
                "evidence_excerpt": "따라서 이 사건 청구는 기각되어야 합니다.",
            },
        )
        self.assertFalse(candidate_result.structured_content["approved"])
        trait_ids.append(candidate_result.structured_content["trait_id"])
        approve_profile(server.store, "lawyer_1", set(trait_ids))

        matter_result = await client.call_tool(
            "create_matter", {"lawyer_id": "lawyer_1", "title": "대여금 사건"}
        )
        matter_id = matter_result.structured_content["matter_id"]
        await client.call_tool(
            "add_case_assertion",
            {
                "lawyer_id": "lawyer_1",
                "matter_id": matter_id,
                "assertion_id": "F1",
                "kind": "fact",
                "text": "원고가 피고에게 금원을 송금하였다.",
                "source_label": "을 제1호증",
            },
        )
        await client.call_tool(
            "add_legal_authority",
            {
                "lawyer_id": "lawyer_1",
                "matter_id": matter_id,
                "authority_id": "A1",
                "citation": "민법 제598조",
                "proposition": "소비대차는 금전 등의 소유권 이전과 반환 약정으로 성립한다.",
                "source_url": "https://law.go.kr",
            },
        )
        await client.call_tool(
            "select_case_perspective",
            {
                "lawyer_id": "lawyer_1",
                "matter_id": matter_id,
                "thesis": "송금만으로 소비대차 합의가 증명되지 않는다.",
                "priorities": ["합의 입증 부족"],
                "risks": ["송금 사실은 인정됨"],
            },
        )
        approve_matter_inputs(
            server.store,
            "lawyer_1",
            matter_id,
            {"F1"},
            {"A1"},
            True,
        )

        context_result = await client.call_tool(
            "prepare_drafting_context",
            {"lawyer_id": "lawyer_1", "matter_id": matter_id},
        )
        context = context_result.structured_content
        self.assertEqual(context["matter"]["assertions"][0]["assertion_id"], "F1")
        packet_id = context["packet_id"]

        await client.call_tool(
            "select_case_perspective",
            {
                "lawyer_id": "lawyer_1",
                "matter_id": matter_id,
                "thesis": "나중에 변경된 관점",
                "priorities": [],
                "risks": [],
            },
        )

        draft_result = await client.call_tool(
            "save_and_verify_draft",
            {
                "lawyer_id": "lawyer_1",
                "matter_id": matter_id,
                "packet_id": packet_id,
                "title": "준비서면 초안",
                "content": (
                    "1. 결론\n\n"
                    "원고의 송금 사실은 인정됩니다. [근거: F1]\n\n"
                    "그러나 송금만으로 소비대차 합의가 증명되지는 않습니다. [법률: A1]"
                ),
            },
        )
        self.assertTrue(draft_result.structured_content["deterministic_passed"])
        self.assertFalse(draft_result.structured_content["filing_ready"])
        self.assertTrue(draft_result.structured_content["requires_human_source_review"])
        draft = server.store.get_draft(
            draft_result.structured_content["draft_id"], "lawyer_1"
        )
        packet = server.store.get_packet(draft.packet_id, "lawyer_1")
        self.assertEqual(packet.packet_id, packet_id)
        self.assertEqual(
            packet.context["selected_perspective"]["thesis"],
            "송금만으로 소비대차 합의가 증명되지 않는다.",
        )

    async def test_workspace_isolation_rejects_other_lawyer(self) -> None:
        previous_workspace = server.WORKSPACE_ID
        server.WORKSPACE_ID = "lawyer_1"
        try:
            async with Client(server.mcp) as client:
                result = await client.call_tool(
                    "create_matter",
                    {"lawyer_id": "lawyer_2", "title": "다른 사건"},
                )
            self.assertTrue(result.is_error)
        finally:
            server.WORKSPACE_ID = previous_workspace
