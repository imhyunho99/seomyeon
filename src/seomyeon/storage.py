from __future__ import annotations

import json
import fcntl
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from .models import (
    ArgumentPlan,
    ArgumentUnit,
    Authority,
    CaseMetadata,
    CaseAssertion,
    ClaimTheory,
    Draft,
    DraftingPacket,
    LawyerProfile,
    LegalElement,
    Matter,
    OpposingPosition,
    Perspective,
    PriorBrief,
    ProfileTrait,
    SourceSpan,
    VerificationFinding,
    hash_context,
    to_dict,
)


class NotFoundError(KeyError):
    pass


class OwnershipError(PermissionError):
    pass


class JsonStore:
    """Small pilot store. IDs are handles, never authorization credentials."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self._lock = RLock()
        with self._lock, self._file_lock(exclusive=True):
            if not self.path.exists():
                self._write_unlocked(
                    {
                        "briefs": {},
                        "profiles": {},
                        "matters": {},
                        "packets": {},
                        "drafts": {},
                    }
                )

    @contextmanager
    def _file_lock(self, *, exclusive: bool):
        with self.lock_path.open("a+") as lock_file:
            os.chmod(self.lock_path, 0o600)
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_file.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("packets", {})
        return data

    def _read(self) -> dict[str, Any]:
        with self._lock, self._file_lock(exclusive=False):
            return self._read_unlocked()

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        temp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.chmod(0o600)
        temp.replace(self.path)
        self.path.chmod(0o600)

    def save_brief(self, brief: PriorBrief) -> None:
        with self._lock, self._file_lock(exclusive=True):
            data = self._read_unlocked()
            data["briefs"][brief.brief_id] = to_dict(brief)
            self._write_unlocked(data)

    def list_briefs(self, lawyer_id: str) -> list[PriorBrief]:
        data = self._read()
        return [
            PriorBrief(**item)
            for item in data["briefs"].values()
            if item["lawyer_id"] == lawyer_id
        ]

    def get_brief(self, brief_id: str, lawyer_id: str) -> PriorBrief:
        data = self._read()
        item = data["briefs"].get(brief_id)
        if item is None:
            raise NotFoundError(f"Brief {brief_id} not found")
        if item["lawyer_id"] != lawyer_id:
            raise OwnershipError("Brief does not belong to this lawyer")
        return PriorBrief(**item)

    def save_profile(self, profile: LawyerProfile) -> None:
        with self._lock, self._file_lock(exclusive=True):
            data = self._read_unlocked()
            data["profiles"][profile.profile_id] = to_dict(profile)
            self._write_unlocked(data)

    def latest_profile(self, lawyer_id: str) -> LawyerProfile:
        data = self._read()
        matches = [
            item for item in data["profiles"].values() if item["lawyer_id"] == lawyer_id
        ]
        if not matches:
            raise NotFoundError(f"No profile for lawyer {lawyer_id}")
        item = max(matches, key=lambda value: value["version"])
        return self._profile_from_item(item)

    @staticmethod
    def _profile_from_item(item: dict[str, Any]) -> LawyerProfile:
        traits = [
            ProfileTrait(
                **{
                    **trait,
                    "evidence": [SourceSpan(**span) for span in trait["evidence"]],
                }
            )
            for trait in item["traits"]
        ]
        return LawyerProfile(
            **{
                **item,
                "traits": traits,
                "operational_rules": item.get("operational_rules", {}),
            }
        )

    def update_latest_profile(
        self, lawyer_id: str, updater: Callable[[LawyerProfile], LawyerProfile]
    ) -> LawyerProfile:
        with self._lock, self._file_lock(exclusive=True):
            data = self._read_unlocked()
            matches = [
                item
                for item in data["profiles"].values()
                if item["lawyer_id"] == lawyer_id
            ]
            if not matches:
                raise NotFoundError(f"No profile for lawyer {lawyer_id}")
            profile = self._profile_from_item(
                max(matches, key=lambda value: value["version"])
            )
            updated = updater(profile)
            if updated.profile_id != profile.profile_id:
                raise ValueError("Profile updater may not change profile_id")
            data["profiles"][updated.profile_id] = to_dict(updated)
            self._write_unlocked(data)
            return updated

    def save_matter(self, matter: Matter) -> None:
        with self._lock, self._file_lock(exclusive=True):
            data = self._read_unlocked()
            data["matters"][matter.matter_id] = to_dict(matter)
            self._write_unlocked(data)

    def get_matter(self, matter_id: str, lawyer_id: str) -> Matter:
        data = self._read()
        item = data["matters"].get(matter_id)
        if item is None:
            raise NotFoundError(f"Matter {matter_id} not found")
        if item["lawyer_id"] != lawyer_id:
            raise OwnershipError("Matter does not belong to this lawyer")
        return self._matter_from_item(item)

    def list_matters(self, lawyer_id: str) -> list[Matter]:
        data = self._read()
        return [
            self._matter_from_item(item)
            for item in data["matters"].values()
            if item["lawyer_id"] == lawyer_id
        ]

    @staticmethod
    def _matter_from_item(item: dict[str, Any]) -> Matter:
        claims = [
            ClaimTheory(
                **{
                    **claim,
                    "elements": [
                        LegalElement(**element) for element in claim.get("elements", [])
                    ],
                }
            )
            for claim in item.get("claims", [])
        ]
        plan_item = item.get("argument_plan")
        return Matter(
            matter_id=item["matter_id"],
            lawyer_id=item["lawyer_id"],
            title=item["title"],
            assertions=[CaseAssertion(**value) for value in item["assertions"]],
            authorities=[Authority(**value) for value in item["authorities"]],
            perspective=Perspective(**item["perspective"])
            if item["perspective"]
            else None,
            case_metadata=CaseMetadata(**item["case_metadata"])
            if item.get("case_metadata")
            else None,
            claims=claims,
            opposing_positions=[
                OpposingPosition(**value)
                for value in item.get("opposing_positions", [])
            ],
            argument_plan=ArgumentPlan(
                units=[ArgumentUnit(**unit) for unit in plan_item.get("units", [])],
                unresolved=plan_item.get("unresolved", []),
                approved=plan_item.get("approved", False),
            )
            if plan_item
            else None,
            created_at=item["created_at"],
        )

    def update_matter(
        self,
        matter_id: str,
        lawyer_id: str,
        updater: Callable[[Matter], Matter],
    ) -> Matter:
        with self._lock, self._file_lock(exclusive=True):
            data = self._read_unlocked()
            item = data["matters"].get(matter_id)
            if item is None:
                raise NotFoundError(f"Matter {matter_id} not found")
            if item["lawyer_id"] != lawyer_id:
                raise OwnershipError("Matter does not belong to this lawyer")
            matter = self._matter_from_item(item)
            updated = updater(matter)
            if updated.matter_id != matter_id or updated.lawyer_id != lawyer_id:
                raise ValueError("Matter updater may not change ownership or matter_id")
            data["matters"][matter_id] = to_dict(updated)
            self._write_unlocked(data)
            return updated

    def save_draft(self, draft: Draft, lawyer_id: str) -> None:
        with self._lock, self._file_lock(exclusive=True):
            data = self._read_unlocked()
            matter = data["matters"].get(draft.matter_id)
            if matter is None:
                raise NotFoundError(f"Matter {draft.matter_id} not found")
            if matter["lawyer_id"] != lawyer_id:
                raise OwnershipError("Matter does not belong to this lawyer")
            packet = data["packets"].get(draft.packet_id)
            if packet is None:
                raise NotFoundError(f"Drafting packet {draft.packet_id} not found")
            if packet["lawyer_id"] != lawyer_id:
                raise OwnershipError("Drafting packet does not belong to this lawyer")
            if packet["matter_id"] != draft.matter_id:
                raise ValueError("Drafting packet does not match the draft matter")
            data["drafts"][draft.draft_id] = to_dict(draft)
            self._write_unlocked(data)

    def save_packet(self, packet: DraftingPacket) -> None:
        with self._lock, self._file_lock(exclusive=True):
            data = self._read_unlocked()
            if packet.packet_id in data["packets"]:
                raise ValueError(f"Duplicate packet_id: {packet.packet_id}")
            data["packets"][packet.packet_id] = to_dict(packet)
            self._write_unlocked(data)

    def get_packet(self, packet_id: str, lawyer_id: str) -> DraftingPacket:
        data = self._read()
        item = data["packets"].get(packet_id)
        if item is None:
            raise NotFoundError(f"Drafting packet {packet_id} not found")
        if item["lawyer_id"] != lawyer_id:
            raise OwnershipError("Drafting packet does not belong to this lawyer")
        if hash_context(item["context"]) != item["context_hash"]:
            raise ValueError("Drafting packet integrity check failed")
        return DraftingPacket(**item)

    def get_draft(self, draft_id: str, lawyer_id: str) -> Draft:
        data = self._read()
        item = data["drafts"].get(draft_id)
        if item is None:
            raise NotFoundError(f"Draft {draft_id} not found")
        self.get_matter(item["matter_id"], lawyer_id)
        return Draft(
            **{
                **item,
                "findings": [
                    VerificationFinding(**finding) for finding in item["findings"]
                ],
            }
        )

    def list_drafts(self, lawyer_id: str) -> list[Draft]:
        data = self._read()
        matter_ids = {
            item["matter_id"]
            for item in data["matters"].values()
            if item["lawyer_id"] == lawyer_id
        }
        return [
            Draft(
                **{
                    **item,
                    "findings": [
                        VerificationFinding(**finding) for finding in item["findings"]
                    ],
                }
            )
            for item in data["drafts"].values()
            if item["matter_id"] in matter_ids
        ]


class SqliteStore:
    """Transactional storage for the multi-account web service."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self._lock = RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS domain_objects (
                    kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    version INTEGER,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (kind, object_id)
                );
                CREATE INDEX IF NOT EXISTS idx_domain_owner_kind
                    ON domain_objects(owner_id, kind, created_at);
                CREATE INDEX IF NOT EXISTS idx_domain_profile_version
                    ON domain_objects(owner_id, kind, version);
                """
            )

    def _secure_database_files(self) -> None:
        for path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._secure_database_files()

    @staticmethod
    def _payload(value: Any) -> str:
        return json.dumps(to_dict(value), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["payload"])

    def _insert(
        self,
        kind: str,
        object_id: str,
        owner_id: str,
        value: Any,
        *,
        version: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO domain_objects (
                    kind, object_id, owner_id, version, created_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    object_id,
                    owner_id,
                    version,
                    value.created_at,
                    self._payload(value),
                ),
            )

    def _row(self, kind: str, object_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM domain_objects WHERE kind = ? AND object_id = ?",
                (kind, object_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"{kind.title()} {object_id} not found")
        return row

    def _owned_row(self, kind: str, object_id: str, owner_id: str) -> sqlite3.Row:
        row = self._row(kind, object_id)
        if row["owner_id"] != owner_id:
            raise OwnershipError(f"{kind.title()} does not belong to this user")
        return row

    def _list(self, kind: str, owner_id: str) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM domain_objects
                WHERE kind = ? AND owner_id = ?
                ORDER BY created_at ASC
                """,
                (kind, owner_id),
            ).fetchall()

    def storage_usage(self, owner_id: str) -> dict[str, int]:
        """Return per-owner object counts and serialized payload bytes."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT kind, COUNT(*) AS object_count,
                       COALESCE(SUM(LENGTH(CAST(payload AS BLOB))), 0) AS payload_bytes
                FROM domain_objects
                WHERE owner_id = ?
                GROUP BY kind
                """,
                (owner_id,),
            ).fetchall()
        usage = {"total_bytes": 0}
        for row in rows:
            usage[f"{row['kind']}_count"] = int(row["object_count"])
            usage["total_bytes"] += int(row["payload_bytes"])
        return usage

    def save_brief(self, brief: PriorBrief) -> None:
        self._insert("brief", brief.brief_id, brief.lawyer_id, brief)

    def list_briefs(self, lawyer_id: str) -> list[PriorBrief]:
        return [
            PriorBrief(**self._decode(row)) for row in self._list("brief", lawyer_id)
        ]

    def get_brief(self, brief_id: str, lawyer_id: str) -> PriorBrief:
        return PriorBrief(**self._decode(self._owned_row("brief", brief_id, lawyer_id)))

    def save_profile(self, profile: LawyerProfile) -> None:
        self._insert(
            "profile",
            profile.profile_id,
            profile.lawyer_id,
            profile,
            version=profile.version,
        )

    def latest_profile(self, lawyer_id: str) -> LawyerProfile:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM domain_objects
                WHERE kind = 'profile' AND owner_id = ?
                ORDER BY version DESC LIMIT 1
                """,
                (lawyer_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"No profile for lawyer {lawyer_id}")
        return JsonStore._profile_from_item(self._decode(row))

    def update_latest_profile(
        self, lawyer_id: str, updater: Callable[[LawyerProfile], LawyerProfile]
    ) -> LawyerProfile:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM domain_objects
                WHERE kind = 'profile' AND owner_id = ?
                ORDER BY version DESC LIMIT 1
                """,
                (lawyer_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"No profile for lawyer {lawyer_id}")
            profile = JsonStore._profile_from_item(self._decode(row))
            updated = updater(profile)
            if updated.profile_id != profile.profile_id:
                raise ValueError("Profile updater may not change profile_id")
            connection.execute(
                "UPDATE domain_objects SET payload = ? WHERE kind = 'profile' AND object_id = ?",
                (self._payload(updated), updated.profile_id),
            )
            return updated

    def save_matter(self, matter: Matter) -> None:
        self._insert("matter", matter.matter_id, matter.lawyer_id, matter)

    def get_matter(self, matter_id: str, lawyer_id: str) -> Matter:
        return JsonStore._matter_from_item(
            self._decode(self._owned_row("matter", matter_id, lawyer_id))
        )

    def list_matters(self, lawyer_id: str) -> list[Matter]:
        return [
            JsonStore._matter_from_item(self._decode(row))
            for row in self._list("matter", lawyer_id)
        ]

    def update_matter(
        self,
        matter_id: str,
        lawyer_id: str,
        updater: Callable[[Matter], Matter],
    ) -> Matter:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM domain_objects WHERE kind = 'matter' AND object_id = ?",
                (matter_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Matter {matter_id} not found")
            if row["owner_id"] != lawyer_id:
                raise OwnershipError("Matter does not belong to this user")
            matter = JsonStore._matter_from_item(self._decode(row))
            updated = updater(matter)
            if updated.matter_id != matter_id or updated.lawyer_id != lawyer_id:
                raise ValueError("Matter updater may not change ownership or matter_id")
            connection.execute(
                "UPDATE domain_objects SET payload = ? WHERE kind = 'matter' AND object_id = ?",
                (self._payload(updated), matter_id),
            )
            return updated

    def save_packet(self, packet: DraftingPacket) -> None:
        self._insert("packet", packet.packet_id, packet.lawyer_id, packet)

    def get_packet(self, packet_id: str, lawyer_id: str) -> DraftingPacket:
        item = self._decode(self._owned_row("packet", packet_id, lawyer_id))
        if hash_context(item["context"]) != item["context_hash"]:
            raise ValueError("Drafting packet integrity check failed")
        return DraftingPacket(**item)

    def save_draft(self, draft: Draft, lawyer_id: str) -> None:
        self.get_matter(draft.matter_id, lawyer_id)
        packet = self.get_packet(draft.packet_id, lawyer_id)
        if packet.matter_id != draft.matter_id:
            raise ValueError("Drafting packet does not match the draft matter")
        self._insert("draft", draft.draft_id, lawyer_id, draft)

    @staticmethod
    def _draft_from_item(item: dict[str, Any]) -> Draft:
        return Draft(
            **{
                **item,
                "findings": [
                    VerificationFinding(**finding) for finding in item["findings"]
                ],
            }
        )

    def get_draft(self, draft_id: str, lawyer_id: str) -> Draft:
        return self._draft_from_item(
            self._decode(self._owned_row("draft", draft_id, lawyer_id))
        )

    def list_drafts(self, lawyer_id: str) -> list[Draft]:
        return [
            self._draft_from_item(self._decode(row))
            for row in self._list("draft", lawyer_id)
        ]

    (LegalElement,)
    (OpposingPosition,)
