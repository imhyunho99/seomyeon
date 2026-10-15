from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

from .services.profile import approve_traits
from .storage import JsonStore


def approve_profile(store: JsonStore, lawyer_id: str, trait_ids: set[str]) -> None:
    def approve(profile):
        known_traits = {item.trait_id for item in profile.traits}
        if unknown := trait_ids - known_traits:
            raise ValueError(f"Unknown trait IDs: {sorted(unknown)}")
        return approve_traits(profile, trait_ids)

    store.update_latest_profile(lawyer_id, approve)


def approve_matter_inputs(
    store: JsonStore,
    lawyer_id: str,
    matter_id: str,
    assertion_ids: set[str],
    authority_ids: set[str],
    approve_perspective: bool,
) -> None:
    def approve(matter):
        known_assertions = {item.assertion_id for item in matter.assertions}
        known_authorities = {item.authority_id for item in matter.authorities}
        if unknown := assertion_ids - known_assertions:
            raise ValueError(f"Unknown assertion IDs: {sorted(unknown)}")
        if unknown := authority_ids - known_authorities:
            raise ValueError(f"Unknown authority IDs: {sorted(unknown)}")
        if approve_perspective and matter.perspective is None:
            raise ValueError("No perspective exists to approve")

        matter.assertions = [
            replace(item, confirmed=True)
            if item.assertion_id in assertion_ids
            else item
            for item in matter.assertions
        ]
        matter.authorities = [
            replace(item, verified=True) if item.authority_id in authority_ids else item
            for item in matter.authorities
        ]
        if approve_perspective and matter.perspective:
            matter.perspective = replace(matter.perspective, approved=True)
        matter.argument_plan = None
        return matter

    store.update_matter(matter_id, lawyer_id, approve)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local human-approval boundary for the Seomyeon pilot"
    )
    parser.add_argument("--lawyer-id", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile_parser = subparsers.add_parser("approve-profile")
    profile_parser.add_argument("trait_ids", nargs="+")

    matter_parser = subparsers.add_parser("approve-matter")
    matter_parser.add_argument("matter_id")
    matter_parser.add_argument("--assertion", action="append", default=[])
    matter_parser.add_argument("--authority", action="append", default=[])
    matter_parser.add_argument("--perspective", action="store_true")

    args = parser.parse_args()
    workspace_id = os.environ.get("SEOMYEON_WORKSPACE_ID")
    if not workspace_id or args.lawyer_id != workspace_id:
        raise PermissionError("--lawyer-id must match SEOMYEON_WORKSPACE_ID")
    data_path = Path(os.environ.get("SEOMYEON_DATA_PATH", "data/seomyeon.json"))
    store = JsonStore(data_path)

    if args.command == "approve-profile":
        approve_profile(store, args.lawyer_id, set(args.trait_ids))
    else:
        approve_matter_inputs(
            store,
            args.lawyer_id,
            args.matter_id,
            set(args.assertion),
            set(args.authority),
            args.perspective,
        )


if __name__ == "__main__":
    main()
