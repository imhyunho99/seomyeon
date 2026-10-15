import tempfile
import unittest
from pathlib import Path

from seomyeon.admin import approve_matter_inputs, approve_profile
from seomyeon.models import (
    Authority,
    CaseAssertion,
    LawyerProfile,
    Matter,
    Perspective,
)
from seomyeon.storage import JsonStore


class AdminApprovalTests(unittest.TestCase):
    def test_human_boundary_approves_only_named_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "store.json")
            matter = Matter(
                matter_id="matter_1",
                lawyer_id="lawyer_1",
                title="사건",
                assertions=[CaseAssertion("F1", "fact", "사실", "증거", False)],
                authorities=[Authority("A1", "법", "명제", "url", False)],
                perspective=Perspective("주장", [], [], False),
            )
            store.save_matter(matter)
            approve_matter_inputs(store, "lawyer_1", "matter_1", {"F1"}, {"A1"}, True)
            approved = store.get_matter("matter_1", "lawyer_1")
            self.assertTrue(approved.assertions[0].confirmed)
            self.assertTrue(approved.authorities[0].verified)
            self.assertTrue(approved.perspective.approved)

    def test_unknown_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "store.json")
            store.save_matter(Matter("matter_1", "lawyer_1", "사건"))
            with self.assertRaisesRegex(ValueError, "Unknown assertion"):
                approve_matter_inputs(
                    store, "lawyer_1", "matter_1", {"F404"}, set(), False
                )

    def test_unknown_profile_trait_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "store.json")
            store.save_profile(LawyerProfile("profile_1", "lawyer_1", 1, []))
            with self.assertRaisesRegex(ValueError, "Unknown trait"):
                approve_profile(store, "lawyer_1", {"trait_404"})
