from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .models import new_id, utc_now

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class AccountError(ValueError):
    pass


class DuplicateAccountError(AccountError):
    pass


@dataclass(frozen=True)
class Account:
    user_id: str
    email: str
    display_name: str
    created_at: str


@dataclass(frozen=True)
class AuthenticatedSession:
    account: Account
    csrf_token: str
    expires_at: int


class AccountStore:
    """SQLite-backed personal accounts and revocable opaque sessions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self._initialize()

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

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    email_normalized TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash BLOB NOT NULL,
                    password_salt BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_web_sessions_user
                    ON web_sessions(user_id, expires_at);
                """
            )

    @staticmethod
    def _normalize_email(email: str) -> str:
        normalized = email.strip().casefold()
        if len(normalized) > 254 or not EMAIL_RE.fullmatch(normalized):
            raise AccountError("올바른 이메일 주소를 입력해 주세요.")
        return normalized

    @staticmethod
    def _hash_password(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )

    def create_account(
        self,
        email: str,
        password: str,
        display_name: str,
        *,
        allow_weak_password: bool = False,
        user_id: str | None = None,
    ) -> Account:
        normalized = self._normalize_email(email)
        if not allow_weak_password and len(password) < 8:
            raise AccountError("비밀번호는 8자 이상이어야 합니다.")
        if len(password) > 256:
            raise AccountError("비밀번호가 너무 깁니다.")
        name = display_name.strip()
        if not name or len(name) > 80:
            raise AccountError("이름은 1자 이상 80자 이하로 입력해 주세요.")
        salt = secrets.token_bytes(16)
        account = Account(user_id or new_id("user"), email.strip(), name, utc_now())
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO users (
                        user_id, email, email_normalized, display_name,
                        password_hash, password_salt, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account.user_id,
                        account.email,
                        normalized,
                        account.display_name,
                        self._hash_password(password, salt),
                        salt,
                        account.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateAccountError("이미 가입된 이메일입니다.") from exc
        return account

    def ensure_seed_account(
        self, email: str, password: str, display_name: str = "테스트 변호사"
    ) -> Account:
        existing = self.get_by_email(email)
        if existing is not None:
            return existing
        return self.create_account(
            email,
            password,
            display_name,
            allow_weak_password=True,
            user_id="test-lawyer" if email.casefold() == "test@test.com" else None,
        )

    def get_by_email(self, email: str) -> Account | None:
        try:
            normalized = self._normalize_email(email)
        except AccountError:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id, email, display_name, created_at FROM users "
                "WHERE email_normalized = ?",
                (normalized,),
            ).fetchone()
        return Account(**dict(row)) if row else None

    def authenticate(self, email: str, password: str) -> Account | None:
        try:
            normalized = self._normalize_email(email)
        except AccountError:
            normalized = "invalid"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email_normalized = ?", (normalized,)
            ).fetchone()
        fallback_salt = b"seomyeon-invalid"
        expected = row["password_hash"] if row else b"\0" * 32
        salt = row["password_salt"] if row else fallback_salt
        actual = self._hash_password(password[:256], salt)
        if row is None or not hmac.compare_digest(actual, expected):
            return None
        return Account(
            row["user_id"], row["email"], row["display_name"], row["created_at"]
        )

    def create_session(
        self, account: Account, *, session_hours: int
    ) -> tuple[str, AuthenticatedSession]:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        csrf_token = secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + session_hours * 3600
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM web_sessions WHERE expires_at < ? OR revoked_at IS NOT NULL",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO web_sessions (
                    token_hash, user_id, csrf_token, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash, account.user_id, csrf_token, expires_at, now),
            )
        return raw_token, AuthenticatedSession(account, csrf_token, expires_at)

    def read_session(self, raw_token: str | None) -> AuthenticatedSession | None:
        if not raw_token:
            return None
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.user_id, u.email, u.display_name, u.created_at,
                       s.csrf_token, s.expires_at
                FROM web_sessions s
                JOIN users u ON u.user_id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at >= ?
                """,
                (token_hash, int(time.time())),
            ).fetchone()
        if row is None:
            return None
        account = Account(
            row["user_id"], row["email"], row["display_name"], row["created_at"]
        )
        return AuthenticatedSession(account, row["csrf_token"], row["expires_at"])

    def revoke_session(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE token_hash = ?",
                (int(time.time()), token_hash),
            )
