from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from seomyeon.admin import approve_matter_inputs
from seomyeon.models import Authority, CaseAssertion, Matter, PriorBrief
from seomyeon.storage import JsonStore, OwnershipError


class StorageTests(unittest.TestCase):
    def test_candidate_and_approval_updates_on_same_matter_are_not_lost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            mcp_store = JsonStore(path)
            admin_store = JsonStore(path)
            mcp_store.save_matter(
                Matter(
                    matter_id="matter_1",
                    lawyer_id="lawyer_1",
                    title="사건",
                    assertions=[CaseAssertion("F1", "fact", "사실", "증거", False)],
                )
            )
            barrier = Barrier(2)

            def approve() -> None:
                barrier.wait()
                approve_matter_inputs(
                    admin_store,
                    "lawyer_1",
                    "matter_1",
                    {"F1"},
                    set(),
                    False,
                )

            def add_authority() -> None:
                barrier.wait()

                def update(matter: Matter) -> Matter:
                    matter.authorities.append(
                        Authority("A1", "민법", "명제", "https://law.go.kr", False)
                    )
                    return matter

                mcp_store.update_matter("matter_1", "lawyer_1", update)

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda fn: fn(), (approve, add_authority)))

            matter = mcp_store.get_matter("matter_1", "lawyer_1")
            self.assertTrue(matter.assertions[0].confirmed)
            self.assertEqual([item.authority_id for item in matter.authorities], ["A1"])

    def test_concurrent_saves_do_not_lose_briefs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            stores = [JsonStore(path), JsonStore(path)]

            def save(index: int) -> None:
                stores[index % len(stores)].save_brief(
                    PriorBrief(
                        brief_id=f"brief_{index}",
                        lawyer_id="lawyer_1",
                        title=str(index),
                        text="본문",
                    )
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(save, range(50)))
            self.assertEqual(len(stores[0].list_briefs("lawyer_1")), 50)

    def test_matter_is_scoped_to_lawyer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "store.json")
            matter = Matter(matter_id="matter_1", lawyer_id="lawyer_1", title="사건")
            store.save_matter(matter)
            self.assertEqual(store.get_matter("matter_1", "lawyer_1").title, "사건")
            with self.assertRaises(OwnershipError):
                store.get_matter("matter_1", "lawyer_2")
