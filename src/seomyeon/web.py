from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .admin import approve_matter_inputs, approve_profile
from .auth import AccountError, AccountStore, DuplicateAccountError
from .ingest import extract_text
from .korean import josa
from .llm import ModelBackend, backend_from_env, read_secret
from .legal_sources import LawOpenDataClient, LegalSourceError, core_holding
from .models import (
    Authority,
    CaseMetadata,
    CaseAssertion,
    ClaimTheory,
    Draft,
    DraftingPacket,
    LegalElement,
    Matter,
    OpposingPosition,
    Perspective,
    PriorBrief,
    hash_context,
    new_id,
    to_dict,
    validate_source_id,
)
from .redaction import names_from_filename, redact
from .services.drafting import DraftingContext, prepare_context
from .services.model_drafting import PROMPT_VERSION, refine_draft, semantic_check
from .services.authority_finder import find_authorities
from .services.argumentation import approve_argument_plan, build_argument_plan
from .services.profile import build_profile
from .services.verification import verify_draft
from .storage import JsonStore, NotFoundError, OwnershipError, SqliteStore

STATIC_DIR = Path(__file__).with_name("web_static")


@dataclass(frozen=True)
class WebConfig:
    production: bool = False
    cookie_secure: bool = False
    session_hours: int = 12
    max_request_bytes: int = 2_500_000
    max_upload_bytes: int = 10 * 1024 * 1024
    max_storage_bytes_per_user: int = 50_000_000
    max_briefs_per_user: int = 100
    max_matters_per_user: int = 100
    max_profiles_per_user: int = 50
    max_drafts_per_user: int = 500
    max_profile_source_chars: int = 10_000_000
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8787",
        "http://localhost:8787",
    )
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")


class WebState:
    def __init__(
        self,
        store: JsonStore | SqliteStore,
        accounts: AccountStore,
        config: WebConfig,
        law_client: LawOpenDataClient | None,
    ):
        self.store = store
        self.accounts = accounts
        self.config = config
        self.law_client = law_client
        self.login_attempts: dict[str, list[float]] = {}
        self.signup_attempts: dict[str, list[float]] = {}
        self.law_search_attempts: dict[str, list[float]] = {}


class QuotaExceededError(ValueError):
    pass


class RateLimitExceededError(ValueError):
    pass


class RequestBodyLimitMiddleware:
    """Bound bodies by bytes even when Content-Length is absent or chunked."""

    def __init__(
        self, app, max_bytes: int, path_limits: dict[str, int] | None = None
    ):
        self.app = app
        self.max_bytes = max_bytes
        self.path_limits = path_limits or {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = self.path_limits.get(scope.get("path", ""), self.max_bytes)
        content_length = dict(scope.get("headers", [])).get(b"content-length")
        try:
            declared_size = int(content_length) if content_length else 0
        except ValueError:
            declared_size = max_bytes + 1
        if declared_size > max_bytes:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > max_bytes:
                await self._reject(scope, receive, send)
                return
            body.extend(chunk)
            more_body = message.get("more_body", False)

        scope.setdefault("state", {})["request_body_bytes"] = len(body)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = JSONResponse(
            {"error": "요청 본문이 허용된 크기를 초과했습니다."}, status_code=413
        )
        await response(scope, receive, send)


def _json_body(request: Request) -> dict[str, Any]:
    return request.state.json_body


async def _parse_json(request: Request) -> None:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 요청 본문을 확인해 주세요.") from exc
    if not isinstance(body, dict):
        raise ValueError("JSON 요청 본문은 객체여야 합니다.")
    request.state.json_body = body


def _require_text(data: dict[str, Any], key: str, *, max_length: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 값이 필요합니다.")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{key} 값은 {max_length}자를 넘을 수 없습니다.")
    return value


def _require_http_url(data: dict[str, Any], key: str) -> str:
    value = _require_text(data, key, max_length=2000)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{key} 값은 유효한 HTTP(S) 주소여야 합니다.")
    return value


def _compose_draft(context: DraftingContext) -> tuple[str, list[str]]:
    plan = context["matter"].get("argument_plan")
    if plan:
        if not plan.get("approved"):
            raise ValueError("승인된 논증계획이 필요합니다.")
        return _compose_structured_draft(context)
    assertions = context["matter"]["assertions"]
    authorities = context["matter"]["authorities"]
    if not assertions:
        raise ValueError("확인된 사건 기록이 한 개 이상 필요합니다.")
    if not authorities:
        raise ValueError("검증된 법률 근거가 한 개 이상 필요합니다.")

    perspective = context["selected_perspective"]
    approved_traits = context["lawyer_profile"]["approved_traits"]
    trait_categories = {item["category"] for item in approved_traits}
    applied_trait_ids = [
        item["trait_id"]
        for item in approved_traits
        if item["category"] in {"reasoning", "guardrail"}
    ]
    fact_refs = " ".join(f"[근거: {item['assertion_id']}]" for item in assertions)
    law_refs = " ".join(f"[법률: {item['authority_id']}]" for item in authorities)
    groups = {
        kind: [item for item in assertions if item["kind"] == kind]
        for kind in ("fact", "disputed_fact", "request", "constraint")
    }
    sections: list[tuple[str, list[str]]] = []
    if groups["fact"]:
        sections.append(
            (
                "확인된 사실관계",
                [
                    f"{item['text']} ({item['source_label']}) "
                    f"[근거: {item['assertion_id']}]"
                    for item in groups["fact"]
                ],
            )
        )
    if groups["disputed_fact"]:
        sections.append(
            (
                "다툼 있는 사실",
                [
                    f"상대방과 다툼이 있는 사항: {item['text']} "
                    f"({item['source_label']}) [근거: {item['assertion_id']}]"
                    for item in groups["disputed_fact"]
                ],
            )
        )
    sections.append(
        (
            "법리",
            [
                f"{item['citation']}에 따르면 {item['proposition']} "
                f"[법률: {item['authority_id']}]"
                for item in authorities
            ],
        )
    )
    priorities = perspective.get("priorities", [])
    priority_text = (
        f"특히 {', '.join(priorities)}의 순서로 검토하면 " if priorities else ""
    )
    sections.append(
        (
            "적용",
            [
                f"{priority_text}위 사건 기록과 법률 근거에 따르면 "
                f"{perspective['thesis']} {fact_refs} {law_refs}"
            ],
        )
    )
    risks = perspective.get("risks", [])
    if risks or groups["constraint"]:
        risk_items = list(risks) + [item["text"] for item in groups["constraint"]]
        sections.append(
            (
                "검토가 필요한 사항",
                [
                    f"다만 {josa(', '.join(risk_items), '은', '는')} 반대 방향의 판단 요소가 될 수 있으므로 "
                    f"최종 제출 전 별도 검토가 필요하다. {fact_refs} {law_refs}"
                ],
            )
        )
    conclusion_lines = [f"{perspective['thesis']} {fact_refs} {law_refs}"]
    if groups["request"]:
        conclusion_lines.extend(
            f"요청 취지: {item['text']} [근거: {item['assertion_id']}]"
            for item in groups["request"]
        )
    conclusion = ("결론", conclusion_lines)
    if "reasoning" in trait_categories:
        sections.insert(0, conclusion)
    else:
        sections.append(conclusion)

    paragraphs: list[str] = []
    for index, (heading, lines) in enumerate(sections, start=1):
        paragraphs.append(f"{index}. {heading}")
        paragraphs.extend(lines)
    return "\n\n".join(paragraphs), applied_trait_ids


def _source_references(unit: dict[str, Any]) -> str:
    references = [f"[근거: {item}]" for item in unit.get("assertion_ids", [])]
    references.extend(f"[법률: {item}]" for item in unit.get("authority_ids", []))
    return " ".join(references)


def _compose_structured_draft(
    context: DraftingContext,
) -> tuple[str, list[str]]:
    matter = context["matter"]
    metadata = matter.get("case_metadata")
    plan = matter.get("argument_plan") or {}
    if not metadata:
        raise ValueError("사건 제출 정보가 필요합니다.")
    units = plan.get("units", [])
    if not units:
        raise ValueError("논증계획에 작성할 문단이 없습니다.")
    rules = context["lawyer_profile"].get("operational_rules", {})
    transition = str(rules.get("preferred_transition") or "따라서")
    traits = context["lawyer_profile"]["approved_traits"]
    used_categories = {"structure", "evidence", "guardrail"}

    cover = "\n".join(
        [
            str(metadata["court"]),
            str(metadata["document_type"]),
            f"사건번호: {metadata['case_number'] or '[확인 필요]'}",
            f"작성 당사자: {metadata['filing_party']}",
            f"상대방: {metadata['opposing_party']}",
            f"청구취지: {metadata['relief_sought']}",
        ]
    )
    body: list[str] = []
    conclusion_units = [unit for unit in units if unit["function"] == "conclusion"]
    argument_units = [unit for unit in units if unit["function"] != "conclusion"]
    if rules.get("conclusion_first") and conclusion_units:
        argument_units = conclusion_units + argument_units
        conclusion_units = []
        used_categories.add("reasoning")

    for index, unit in enumerate(argument_units + conclusion_units, start=1):
        proposition = str(unit["proposition"]).strip()
        if unit["function"] in {"application", "rebuttal"}:
            proposition = f"{transition}, {proposition}"
            used_categories.add("style")
        references = _source_references(unit)
        body.append(f"{index}. {unit['heading']}\n\n{proposition} {references}".strip())

    applied_trait_ids = [
        item["trait_id"] for item in traits if item["category"] in used_categories
    ]
    return "\n\n".join([cover, *body]), applied_trait_ids


def create_app(
    store: JsonStore | SqliteStore,
    config: WebConfig,
    accounts: AccountStore,
    law_client: LawOpenDataClient | None = None,
    model_backend: ModelBackend | None = None,
) -> Starlette:
    state = WebState(store, accounts, config, law_client)

    def user_id(request: Request) -> str:
        return request.state.auth_session.account.user_id

    def user_payload(auth_session) -> dict[str, str]:
        account = auth_session.account
        return {
            "user_id": account.user_id,
            "email": account.email,
            "display_name": account.display_name,
        }

    def set_session_cookie(response: Response, token: str) -> None:
        cookie_name = (
            "__Host-seomyeon_session" if config.production else "seomyeon_session"
        )
        response.set_cookie(
            cookie_name,
            token,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
            max_age=config.session_hours * 3600,
            path="/",
        )

    def session_token(request: Request) -> str | None:
        cookie_name = (
            "__Host-seomyeon_session" if config.production else "seomyeon_session"
        )
        return request.cookies.get(cookie_name)

    def ensure_quota(
        request: Request,
        owner_id: str,
        *,
        kind: str | None = None,
        max_count: int | None = None,
        additional_bytes: int | None = None,
    ) -> None:
        if not isinstance(store, SqliteStore):
            return
        usage = store.storage_usage(owner_id)
        incoming = (
            additional_bytes
            if additional_bytes is not None
            else int(getattr(request.state, "request_body_bytes", 0))
        )
        if usage["total_bytes"] + incoming > config.max_storage_bytes_per_user:
            raise QuotaExceededError("계정 저장 용량을 초과했습니다.")
        if kind and max_count is not None:
            if usage.get(f"{kind}_count", 0) >= max_count:
                raise QuotaExceededError(
                    f"계정의 {kind} 저장 개수 한도를 초과했습니다."
                )

    def origin_allowed(request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin in config.allowed_origins

    async def homepage(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html")

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in {"app.css", "app.js"}:
            return Response(status_code=404)
        return FileResponse(STATIC_DIR / name)

    async def session(request: Request) -> Response:
        token = session_token(request)
        auth_session = accounts.read_session(token)
        return JSONResponse(
            {
                "authenticated": auth_session is not None,
                "user": user_payload(auth_session) if auth_session else None,
                "csrf_token": auth_session.csrf_token if auth_session else None,
            }
        )

    async def login(request: Request) -> Response:
        if not origin_allowed(request):
            return JSONResponse(
                {"error": "허용되지 않은 요청 출처입니다."}, status_code=403
            )
        try:
            await _parse_json(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        data = _json_body(request)
        client = request.client.host if request.client else "local"
        now = time.time()
        email = str(data.get("email", "")).strip()
        attempt_key = f"{client}:{email.casefold()}"
        attempts = [
            value
            for value in state.login_attempts.get(attempt_key, [])
            if now - value < 60
        ]
        if len(attempts) >= 5:
            return JSONResponse(
                {"error": "잠시 후 다시 시도해 주세요."}, status_code=429
            )
        password = str(data.get("password", ""))
        account = accounts.authenticate(email, password)
        if account is None:
            attempts.append(now)
            state.login_attempts[attempt_key] = attempts
            return JSONResponse(
                {"error": "이메일 또는 비밀번호를 확인해 주세요."}, status_code=401
            )
        state.login_attempts.pop(attempt_key, None)
        token, auth_session = accounts.create_session(
            account, session_hours=config.session_hours
        )
        response = JSONResponse(
            {
                "authenticated": True,
                "user": user_payload(auth_session),
                "csrf_token": auth_session.csrf_token,
            }
        )
        set_session_cookie(response, token)
        return response

    async def signup(request: Request) -> Response:
        if not origin_allowed(request):
            return JSONResponse(
                {"error": "허용되지 않은 요청 출처입니다."}, status_code=403
            )
        client = request.client.host if request.client else "local"
        now = time.time()
        attempts = [
            value for value in state.signup_attempts.get(client, []) if now - value < 60
        ]
        if len(attempts) >= 5:
            return JSONResponse(
                {"error": "잠시 후 다시 시도해 주세요."}, status_code=429
            )
        attempts.append(now)
        state.signup_attempts[client] = attempts
        try:
            await _parse_json(request)
            data = _json_body(request)
            account = accounts.create_account(
                _require_text(data, "email", max_length=254),
                _require_text(data, "password", max_length=256),
                _require_text(data, "display_name", max_length=80),
            )
        except (AccountError, ValueError) as exc:
            status = 409 if isinstance(exc, DuplicateAccountError) else 400
            return JSONResponse({"error": str(exc)}, status_code=status)
        token, auth_session = accounts.create_session(
            account, session_hours=config.session_hours
        )
        response = JSONResponse(
            {
                "authenticated": True,
                "user": user_payload(auth_session),
                "csrf_token": auth_session.csrf_token,
            },
            status_code=201,
        )
        set_session_cookie(response, token)
        return response

    async def logout(request: Request) -> Response:
        token = session_token(request)
        accounts.revoke_session(token)
        response = JSONResponse({"authenticated": False})
        cookie_name = (
            "__Host-seomyeon_session" if config.production else "seomyeon_session"
        )
        response.delete_cookie(cookie_name, path="/", secure=config.cookie_secure)
        return response

    async def workspace(request: Request) -> Response:
        owner_id = user_id(request)
        try:
            profile = store.latest_profile(owner_id)
            profile_data = to_dict(profile)
        except NotFoundError:
            profile_data = None
        matters = sorted(
            store.list_matters(owner_id),
            key=lambda item: item.created_at,
            reverse=True,
        )
        drafts = sorted(
            store.list_drafts(owner_id),
            key=lambda item: item.created_at,
            reverse=True,
        )
        return JSONResponse(
            {
                "briefs": [
                    {"brief_id": item.brief_id, "title": item.title}
                    for item in store.list_briefs(owner_id)
                ],
                "profile": profile_data,
                "matters": [to_dict(item) for item in matters],
                "drafts": [to_dict(item) for item in drafts],
            }
        )

    async def add_brief(request: Request) -> Response:
        await _parse_json(request)
        ensure_quota(
            request,
            user_id(request),
            kind="brief",
            max_count=config.max_briefs_per_user,
        )
        data = _json_body(request)
        return save_brief(
            request,
            title=_require_text(data, "title", max_length=160),
            text=_require_text(data, "text", max_length=2_000_000),
            pseudonymize=data.get("pseudonymize", True) is not False,
        )

    def save_brief(
        request: Request,
        *,
        title: str,
        text: str,
        pseudonymize: bool,
        extra_names: list[str] | None = None,
    ) -> Response:
        summary = {"applied": pseudonymize, "names": 0, "identifiers": 0}
        if pseudonymize:
            names = list(extra_names or []) + names_from_filename(title)
            result = redact(text, extra_names=names)
            text, title = result.text, result.apply(title)
            # 실명→가명 대응표는 저장하지도 돌려주지도 않는다
            summary["names"] = len(result.replacements)
            summary["identifiers"] = result.identifier_count
        brief = PriorBrief(
            brief_id=new_id("brief"),
            lawyer_id=user_id(request),
            title=title[:160],
            text=text,
        )
        store.save_brief(brief)
        return JSONResponse({**to_dict(brief), "redaction": summary}, status_code=201)

    async def upload_brief(request: Request) -> Response:
        ensure_quota(
            request,
            user_id(request),
            kind="brief",
            max_count=config.max_briefs_per_user,
        )
        filename = unquote(request.headers.get("x-filename", "")).strip()
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or len(filename) > 255:
            raise ValueError("파일 이름이 필요합니다.")
        data = await request.body()
        text = await asyncio.to_thread(extract_text, filename, data)
        # 저장되는 것은 원본 파일이 아니라 추출된 텍스트다
        ensure_quota(
            request, user_id(request), additional_bytes=len(text.encode("utf-8"))
        )
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        return save_brief(
            request,
            title=stem,
            text=text,
            pseudonymize=request.query_params.get("pseudonymize", "1") != "0",
        )

    async def profile_build(request: Request) -> Response:
        owner_id = user_id(request)
        ensure_quota(
            request,
            owner_id,
            kind="profile",
            max_count=config.max_profiles_per_user,
        )
        briefs = store.list_briefs(owner_id)
        if sum(len(brief.text) for brief in briefs) > config.max_profile_source_chars:
            raise QuotaExceededError(
                "작성 원칙 분석 대상이 너무 큽니다. 서면을 나누거나 일부를 제외해 주세요."
            )
        try:
            version = store.latest_profile(owner_id).version + 1
        except NotFoundError:
            version = 1
        profile = await asyncio.to_thread(build_profile, owner_id, briefs, version)
        ensure_quota(
            request,
            owner_id,
            additional_bytes=len(
                json.dumps(to_dict(profile), ensure_ascii=False).encode("utf-8")
            ),
        )
        store.save_profile(profile)
        return JSONResponse(to_dict(profile), status_code=201)

    async def profile_approve(request: Request) -> Response:
        await _parse_json(request)
        submitted = _json_body(request).get("trait_ids", [])
        if not isinstance(submitted, list):
            raise ValueError("trait_ids 값은 목록이어야 합니다.")
        trait_ids = {str(item) for item in submitted}
        owner_id = user_id(request)
        approve_profile(store, owner_id, trait_ids)
        return JSONResponse(to_dict(store.latest_profile(owner_id)))

    async def matter_create(request: Request) -> Response:
        await _parse_json(request)
        ensure_quota(
            request,
            user_id(request),
            kind="matter",
            max_count=config.max_matters_per_user,
        )
        matter = Matter(
            matter_id=new_id("matter"),
            lawyer_id=user_id(request),
            title=_require_text(_json_body(request), "title", max_length=160),
        )
        store.save_matter(matter)
        return JSONResponse(to_dict(matter), status_code=201)

    async def assertion_add(request: Request) -> Response:
        await _parse_json(request)
        ensure_quota(request, user_id(request))
        data = _json_body(request)
        assertion_id = validate_source_id(
            _require_text(data, "assertion_id", max_length=64)
        )
        kind = data.get("kind")
        if kind not in {"fact", "disputed_fact", "request", "constraint"}:
            raise ValueError("kind 값을 확인해 주세요.")
        assertion = CaseAssertion(
            assertion_id,
            kind,
            _require_text(data, "text", max_length=4000),
            _require_text(data, "source_label", max_length=300),
            False,
        )

        def update(matter: Matter) -> Matter:
            if any(item.assertion_id == assertion_id for item in matter.assertions):
                raise ValueError("같은 사건 기록 ID가 이미 있습니다.")
            matter.assertions.append(assertion)
            matter.argument_plan = None
            return matter

        store.update_matter(request.path_params["matter_id"], user_id(request), update)
        return JSONResponse(to_dict(assertion), status_code=201)

    async def authority_add(request: Request) -> Response:
        await _parse_json(request)
        ensure_quota(request, user_id(request))
        data = _json_body(request)
        authority_id = validate_source_id(
            _require_text(data, "authority_id", max_length=64)
        )
        authority = Authority(
            authority_id,
            _require_text(data, "citation", max_length=500),
            _require_text(data, "proposition", max_length=4000),
            _require_http_url(data, "source_url"),
            False,
        )

        def update(matter: Matter) -> Matter:
            if any(item.authority_id == authority_id for item in matter.authorities):
                raise ValueError("같은 법률 근거 ID가 이미 있습니다.")
            matter.authorities.append(authority)
            matter.argument_plan = None
            return matter

        store.update_matter(request.path_params["matter_id"], user_id(request), update)
        return JSONResponse(to_dict(authority), status_code=201)

    def check_law_rate_limit(owner_id: str, weight: int = 1) -> None:
        now = time.time()
        attempts = [
            value
            for value in state.law_search_attempts.get(owner_id, [])
            if now - value < 60
        ]
        if len(attempts) + weight > 30:
            raise RateLimitExceededError(
                "법률정보 검색 요청이 많습니다. 잠시 후 다시 시도해 주세요."
            )
        attempts.extend([now] * weight)
        state.law_search_attempts[owner_id] = attempts

    async def authority_suggest(request: Request) -> Response:
        if state.law_client is None:
            return JSONResponse(
                {"error": "법률정보 API 인증값(SEOMYEON_LAW_API_OC)이 설정되지 않았습니다."},
                status_code=503,
            )
        owner_id = user_id(request)
        # 자동 찾기는 법률정보 서버를 여러 번 부르므로 일반 검색 10회분으로 센다
        check_law_rate_limit(owner_id, weight=10)
        matter = store.get_matter(request.path_params["matter_id"], owner_id)
        suggestions = await asyncio.to_thread(
            find_authorities, matter, state.law_client, model_backend
        )
        return JSONResponse(
            {
                "provider": "국가법령정보센터",
                "model": model_backend.name if model_backend else "none",
                **to_dict(suggestions),
            }
        )

    async def precedent_search(request: Request) -> Response:
        if state.law_client is None:
            return JSONResponse(
                {
                    "error": (
                        "판례 검색이 아직 연결되지 않았습니다. "
                        "SEOMYEON_LAW_API_OC를 설정해 주세요."
                    )
                },
                status_code=503,
            )
        owner_id = user_id(request)
        check_law_rate_limit(owner_id)
        query = request.query_params.get("query", "")
        results = await asyncio.to_thread(
            state.law_client.search_precedents, query, display=5
        )
        return JSONResponse(
            {
                "provider": "국가법령정보센터",
                "official": True,
                "query": query,
                "results": [to_dict(item) for item in results],
            }
        )

    async def statute_search(request: Request) -> Response:
        if state.law_client is None:
            return JSONResponse(
                {"error": "법령 검색 API 인증값이 설정되지 않았습니다."},
                status_code=503,
            )
        check_law_rate_limit(user_id(request))
        query = request.query_params.get("query", "")
        results = await asyncio.to_thread(
            state.law_client.search_statutes, query, display=5
        )
        return JSONResponse(
            {
                "provider": "국가법령정보센터",
                "official": True,
                "query": query,
                "results": [to_dict(item) for item in results],
            }
        )

    async def precedent_import(request: Request) -> Response:
        if state.law_client is None:
            return JSONResponse(
                {"error": "판례 검색 API 인증값이 설정되지 않았습니다."},
                status_code=503,
            )
        await _parse_json(request)
        owner_id = user_id(request)
        ensure_quota(request, owner_id)
        data = _json_body(request)
        authority_id = validate_source_id(
            _require_text(data, "authority_id", max_length=64)
        )
        precedent_id = _require_text(data, "precedent_id", max_length=20)
        detail = await asyncio.to_thread(state.law_client.get_precedent, precedent_id)
        date = detail.decision_date
        if len(date) == 8 and date.isdigit():
            date = f"{int(date[:4])}. {int(date[4:6])}. {int(date[6:])}."
        citation = " ".join(
            value
            for value in (
                detail.court_name,
                date,
                detail.decision_type,
                detail.case_number,
            )
            if value
        )[:500]
        authority = Authority(
            authority_id=authority_id,
            citation=citation,
            proposition=core_holding(detail.holding),
            source_url=detail.source_url,
            verified=False,
            official_source="국가법령정보센터",
            official_id=detail.precedent_id,
            retrieved_at=detail.retrieved_at,
            content_hash=detail.content_hash,
        )

        def update(matter: Matter) -> Matter:
            if any(item.authority_id == authority_id for item in matter.authorities):
                raise ValueError("같은 법률 근거 ID가 이미 있습니다.")
            if any(
                item.official_id == detail.precedent_id
                for item in matter.authorities
                if item.official_id
            ):
                raise ValueError("이미 이 사건에 추가된 판례입니다.")
            matter.authorities.append(authority)
            matter.argument_plan = None
            return matter

        store.update_matter(request.path_params["matter_id"], owner_id, update)
        return JSONResponse(
            {**to_dict(authority), "issues": detail.issues}, status_code=201
        )

    async def statute_import(request: Request) -> Response:
        if state.law_client is None:
            return JSONResponse(
                {"error": "법령 검색 API 인증값이 설정되지 않았습니다."},
                status_code=503,
            )
        await _parse_json(request)
        owner_id = user_id(request)
        ensure_quota(request, owner_id)
        data = _json_body(request)
        authority_id = validate_source_id(
            _require_text(data, "authority_id", max_length=64)
        )
        mst = _require_text(data, "mst", max_length=20)
        article_number = _require_text(data, "article_number", max_length=20)
        detail = await asyncio.to_thread(
            state.law_client.get_statute_article, mst, article_number
        )
        authority = Authority(
            authority_id=authority_id,
            citation=f"{detail.name} 제{detail.article_number}조",
            proposition=detail.article_text[:4000],
            source_url=detail.source_url,
            verified=False,
            official_source="국가법령정보센터",
            official_id=f"law:{detail.mst}:{detail.article_number}",
            retrieved_at=detail.retrieved_at,
            content_hash=detail.content_hash,
        )

        def update(matter: Matter) -> Matter:
            if any(item.authority_id == authority_id for item in matter.authorities):
                raise ValueError("같은 법률 근거 ID가 이미 있습니다.")
            if any(
                item.official_id == authority.official_id
                for item in matter.authorities
                if item.official_id
            ):
                raise ValueError("이미 이 사건에 추가된 법령 조문입니다.")
            matter.authorities.append(authority)
            matter.argument_plan = None
            return matter

        store.update_matter(request.path_params["matter_id"], owner_id, update)
        return JSONResponse(to_dict(authority), status_code=201)

    async def case_structure_set(request: Request) -> Response:
        await _parse_json(request)
        data = _json_body(request)
        raw_metadata = data.get("case_metadata")
        raw_claims = data.get("claims")
        raw_positions = data.get("opposing_positions", [])
        if not isinstance(raw_metadata, dict):
            raise ValueError("case_metadata 값은 객체여야 합니다.")
        if not isinstance(raw_claims, list) or not isinstance(raw_positions, list):
            raise ValueError("claims와 opposing_positions 값은 목록이어야 합니다.")
        if len(raw_claims) > 20 or len(raw_positions) > 100:
            raise ValueError(
                "청구·항변 또는 상대방 주장 개수가 허용 범위를 넘었습니다."
            )
        metadata = CaseMetadata(
            court=_require_text(raw_metadata, "court", max_length=200),
            case_number=str(raw_metadata.get("case_number", "")).strip()[:100],
            document_type=_require_text(raw_metadata, "document_type", max_length=100),
            filing_party=_require_text(raw_metadata, "filing_party", max_length=300),
            opposing_party=_require_text(
                raw_metadata, "opposing_party", max_length=300
            ),
            relief_sought=_require_text(raw_metadata, "relief_sought", max_length=2000),
        )
        claims: list[ClaimTheory] = []
        claim_ids: set[str] = set()
        element_ids: set[str] = set()
        for raw_claim in raw_claims:
            if not isinstance(raw_claim, dict):
                raise ValueError("각 청구·항변은 객체여야 합니다.")
            raw_elements = raw_claim.get("elements", [])
            if not isinstance(raw_elements, list) or len(raw_elements) > 20:
                raise ValueError("요건사실 목록을 확인해 주세요.")
            claim_id = validate_source_id(
                _require_text(raw_claim, "claim_id", max_length=64)
            )
            if claim_id in claim_ids:
                raise ValueError("청구·항변 ID는 중복될 수 없습니다.")
            claim_ids.add(claim_id)
            elements: list[LegalElement] = []
            for raw_element in raw_elements:
                if not isinstance(raw_element, dict):
                    raise ValueError("각 요건사실은 객체여야 합니다.")
                conclusion = raw_element.get("conclusion")
                if conclusion not in {"satisfied", "not_satisfied", "disputed"}:
                    raise ValueError("요건 충족 판단을 확인해 주세요.")
                assertion_ids = raw_element.get("assertion_ids", [])
                authority_ids = raw_element.get("authority_ids", [])
                if not isinstance(assertion_ids, list) or not isinstance(
                    authority_ids, list
                ):
                    raise ValueError("요건의 근거 ID는 목록이어야 합니다.")
                element_id = validate_source_id(
                    _require_text(raw_element, "element_id", max_length=64)
                )
                if element_id in element_ids:
                    raise ValueError("요건 ID는 중복될 수 없습니다.")
                element_ids.add(element_id)
                elements.append(
                    LegalElement(
                        element_id=element_id,
                        name=_require_text(raw_element, "name", max_length=300),
                        legal_test=_require_text(
                            raw_element, "legal_test", max_length=2000
                        ),
                        burden_party=_require_text(
                            raw_element, "burden_party", max_length=300
                        ),
                        conclusion=conclusion,
                        assertion_ids=[
                            validate_source_id(str(item)) for item in assertion_ids
                        ],
                        authority_ids=[
                            validate_source_id(str(item)) for item in authority_ids
                        ],
                    )
                )
            position = raw_claim.get("position")
            if position not in {"claim", "defense"}:
                raise ValueError("청구·항변 구분을 확인해 주세요.")
            claims.append(
                ClaimTheory(
                    claim_id=claim_id,
                    label=_require_text(raw_claim, "label", max_length=300),
                    position=position,
                    requested_relief=_require_text(
                        raw_claim, "requested_relief", max_length=2000
                    ),
                    elements=elements,
                )
            )
        positions: list[OpposingPosition] = []
        position_ids: set[str] = set()
        for raw_position in raw_positions:
            if not isinstance(raw_position, dict):
                raise ValueError("각 상대방 주장은 객체여야 합니다.")
            response = raw_position.get("response")
            if response not in {"admit", "deny", "lack_knowledge", "partial"}:
                raise ValueError("상대방 주장에 대한 답변 구분을 확인해 주세요.")
            assertion_ids = raw_position.get("assertion_ids", [])
            authority_ids = raw_position.get("authority_ids", [])
            if not isinstance(assertion_ids, list) or not isinstance(
                authority_ids, list
            ):
                raise ValueError("답변 근거 ID는 목록이어야 합니다.")
            position_id = validate_source_id(
                _require_text(raw_position, "position_id", max_length=64)
            )
            if position_id in position_ids:
                raise ValueError("상대방 주장 ID는 중복될 수 없습니다.")
            position_ids.add(position_id)
            positions.append(
                OpposingPosition(
                    position_id=position_id,
                    allegation=_require_text(
                        raw_position, "allegation", max_length=4000
                    ),
                    response=response,
                    response_text=str(raw_position.get("response_text", "")).strip()[
                        :4000
                    ],
                    assertion_ids=[
                        validate_source_id(str(item)) for item in assertion_ids
                    ],
                    authority_ids=[
                        validate_source_id(str(item)) for item in authority_ids
                    ],
                )
            )

        def update(matter: Matter) -> Matter:
            matter.case_metadata = metadata
            matter.claims = claims
            matter.opposing_positions = positions
            matter.argument_plan = None
            return matter

        matter = store.update_matter(
            request.path_params["matter_id"], user_id(request), update
        )
        return JSONResponse(to_dict(matter))

    async def argument_plan_build(request: Request) -> Response:
        matter_id = request.path_params["matter_id"]
        owner_id = user_id(request)

        def update(matter: Matter) -> Matter:
            matter.argument_plan = build_argument_plan(matter)
            return matter

        matter = store.update_matter(matter_id, owner_id, update)
        return JSONResponse(to_dict(matter.argument_plan), status_code=201)

    async def argument_plan_approve(request: Request) -> Response:
        await _parse_json(request)
        if _json_body(request).get("approve") is not True:
            raise ValueError("논증계획 승인 여부를 확인해 주세요.")

        def update(matter: Matter) -> Matter:
            if matter.argument_plan is None:
                raise ValueError("먼저 논증계획을 생성해 주세요.")
            matter.argument_plan = approve_argument_plan(matter.argument_plan)
            return matter

        matter = store.update_matter(
            request.path_params["matter_id"], user_id(request), update
        )
        return JSONResponse(to_dict(matter.argument_plan))

    async def perspective_set(request: Request) -> Response:
        await _parse_json(request)
        ensure_quota(request, user_id(request))
        data = _json_body(request)
        priorities = data.get("priorities", [])
        risks = data.get("risks", [])
        if not isinstance(priorities, list) or not isinstance(risks, list):
            raise ValueError("우선순위와 위험요소 형식을 확인해 주세요.")
        perspective = Perspective(
            _require_text(data, "thesis", max_length=2000),
            [str(item)[:500] for item in priorities],
            [str(item)[:500] for item in risks],
            False,
        )

        def update(matter: Matter) -> Matter:
            matter.perspective = perspective
            matter.argument_plan = None
            return matter

        store.update_matter(request.path_params["matter_id"], user_id(request), update)
        return JSONResponse(to_dict(perspective))

    async def matter_approve(request: Request) -> Response:
        await _parse_json(request)
        data = _json_body(request)
        assertion_ids = data.get("assertion_ids", [])
        authority_ids = data.get("authority_ids", [])
        if not isinstance(assertion_ids, list) or not isinstance(authority_ids, list):
            raise ValueError("승인할 근거 ID는 목록이어야 합니다.")
        matter_id = request.path_params["matter_id"]
        owner_id = user_id(request)
        approve_perspective = data.get("approve_perspective")
        if not isinstance(approve_perspective, bool):
            raise ValueError("approve_perspective 값은 참/거짓이어야 합니다.")
        approve_matter_inputs(
            store,
            owner_id,
            matter_id,
            {str(item) for item in assertion_ids},
            {str(item) for item in authority_ids},
            approve_perspective,
        )
        return JSONResponse(to_dict(store.get_matter(matter_id, owner_id)))

    async def draft_create(request: Request) -> Response:
        await _parse_json(request)
        matter_id = request.path_params["matter_id"]
        owner_id = user_id(request)
        ensure_quota(
            request,
            owner_id,
            kind="draft",
            max_count=config.max_drafts_per_user,
        )
        matter = store.get_matter(matter_id, owner_id)
        profile = store.latest_profile(owner_id)
        context = prepare_context(profile, matter)
        packet = DraftingPacket(
            packet_id=new_id("packet"),
            lawyer_id=owner_id,
            matter_id=matter_id,
            profile_id=profile.profile_id,
            context_hash=hash_context(context),
            context=context,
        )
        content, applied_trait_ids = _compose_draft(context)
        generation: dict[str, Any] = {"model": "deterministic"}
        model_findings = []
        if model_backend is not None:
            refined = await asyncio.to_thread(
                refine_draft, content, profile.operational_rules, model_backend
            )
            content = refined.content
            model_findings = refined.findings + await asyncio.to_thread(
                semantic_check, content, matter, model_backend
            )
            generation = {
                "model": model_backend.name,
                "prompt_version": PROMPT_VERSION,
                "rewritten_paragraphs": refined.rewritten,
                "rejected_paragraphs": refined.rejected,
            }
        findings = verify_draft(content, matter) + model_findings
        draft = Draft(
            draft_id=new_id("draft"),
            matter_id=matter_id,
            profile_id=profile.profile_id,
            packet_id=packet.packet_id,
            title=str(
                _json_body(request).get("title") or f"{matter.title} 준비서면 초안"
            )[:160],
            content=content,
            findings=findings,
            generation=generation,
        )
        generated_bytes = len(
            json.dumps(to_dict(packet), ensure_ascii=False).encode("utf-8")
        ) + len(json.dumps(to_dict(draft), ensure_ascii=False).encode("utf-8"))
        ensure_quota(
            request,
            owner_id,
            additional_bytes=generated_bytes,
        )
        store.save_packet(packet)
        store.save_draft(draft, owner_id)
        return JSONResponse(
            {
                **to_dict(draft),
                "deterministic_passed": not any(
                    item.severity == "high" for item in findings
                ),
                "filing_ready": False,
                "applied_trait_ids": applied_trait_ids,
            },
            status_code=201,
        )

    async def guarded(request: Request, endpoint) -> Response:
        token = session_token(request)
        auth_session = accounts.read_session(token)
        if auth_session is None:
            return JSONResponse({"error": "로그인이 필요합니다."}, status_code=401)
        request.state.auth_session = auth_session
        if request.method not in {"GET", "HEAD"}:
            if not origin_allowed(request):
                return JSONResponse(
                    {"error": "허용되지 않은 요청 출처입니다."}, status_code=403
                )
            supplied = request.headers.get("x-csrf-token", "")
            expected = auth_session.csrf_token
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(
                    {"error": "요청 보안 토큰이 만료되었습니다."}, status_code=403
                )
        try:
            return await endpoint(request)
        except OwnershipError:
            return JSONResponse(
                {"error": "요청한 자료를 찾을 수 없습니다."}, status_code=404
            )
        except QuotaExceededError as exc:
            return JSONResponse({"error": str(exc)}, status_code=507)
        except RateLimitExceededError as exc:
            return JSONResponse({"error": str(exc)}, status_code=429)
        except LegalSourceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except (ValueError, NotFoundError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    def protect(endpoint):
        async def wrapped(request: Request) -> Response:
            return await guarded(request, endpoint)

        return wrapped

    routes = [
        Route("/", homepage),
        Route("/assets/{name}", asset),
        Route("/api/session", session),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/signup", signup, methods=["POST"]),
        Route("/api/logout", protect(logout), methods=["POST"]),
        Route("/api/workspace", protect(workspace)),
        Route("/api/briefs", protect(add_brief), methods=["POST"]),
        Route("/api/briefs/upload", protect(upload_brief), methods=["POST"]),
        Route("/api/profile/build", protect(profile_build), methods=["POST"]),
        Route("/api/profile/approve", protect(profile_approve), methods=["POST"]),
        Route("/api/matters", protect(matter_create), methods=["POST"]),
        Route(
            "/api/matters/{matter_id}/assertions",
            protect(assertion_add),
            methods=["POST"],
        ),
        Route(
            "/api/matters/{matter_id}/authorities",
            protect(authority_add),
            methods=["POST"],
        ),
        Route("/api/legal/precedents", protect(precedent_search)),
        Route("/api/legal/statutes", protect(statute_search)),
        Route(
            "/api/matters/{matter_id}/authorities/suggest",
            protect(authority_suggest),
            methods=["POST"],
        ),
        Route(
            "/api/matters/{matter_id}/authorities/import-precedent",
            protect(precedent_import),
            methods=["POST"],
        ),
        Route(
            "/api/matters/{matter_id}/authorities/import-statute",
            protect(statute_import),
            methods=["POST"],
        ),
        Route(
            "/api/matters/{matter_id}/case-structure",
            protect(case_structure_set),
            methods=["PUT"],
        ),
        Route(
            "/api/matters/{matter_id}/argument-plan",
            protect(argument_plan_build),
            methods=["POST"],
        ),
        Route(
            "/api/matters/{matter_id}/argument-plan/approve",
            protect(argument_plan_approve),
            methods=["POST"],
        ),
        Route(
            "/api/matters/{matter_id}/perspective",
            protect(perspective_set),
            methods=["PUT"],
        ),
        Route(
            "/api/matters/{matter_id}/approve",
            protect(matter_approve),
            methods=["POST"],
        ),
        Route(
            "/api/matters/{matter_id}/draft",
            protect(draft_create),
            methods=["POST"],
        ),
    ]

    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["Cache-Control"] = "no-store"
        if config.cookie_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    app = Starlette(routes=routes)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=config.max_request_bytes,
        path_limits={"/api/briefs/upload": config.max_upload_bytes},
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts))
    return app


def _config_from_env() -> WebConfig:
    production = (
        os.environ.get("SEOMYEON_ENV", "development").casefold() == "production"
    )
    origins = tuple(
        item.strip()
        for item in os.environ.get(
            "SEOMYEON_ALLOWED_ORIGINS",
            "http://127.0.0.1:8787,http://localhost:8787",
        ).split(",")
        if item.strip()
    )
    hosts = tuple(
        item.strip()
        for item in os.environ.get(
            "SEOMYEON_ALLOWED_HOSTS", "127.0.0.1,localhost"
        ).split(",")
        if item.strip()
    )
    return WebConfig(
        production=production,
        cookie_secure=os.environ.get("SEOMYEON_COOKIE_SECURE") == "1",
        session_hours=int(os.environ.get("SEOMYEON_SESSION_HOURS", "12")),
        allowed_origins=origins,
        allowed_hosts=hosts,
    )


def _validate_runtime_config(config: WebConfig) -> None:
    if not config.production:
        return
    if not config.cookie_secure:
        raise RuntimeError("운영 환경은 SEOMYEON_COOKIE_SECURE=1이 필요합니다.")
    if not config.allowed_origins or any(
        (
            (parsed := urlparse(origin)).scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        )
        for origin in config.allowed_origins
    ):
        raise RuntimeError("운영 환경의 모든 허용 Origin은 HTTPS여야 합니다.")
    if not config.allowed_hosts or "*" in config.allowed_hosts:
        raise RuntimeError("운영 환경은 명시적인 허용 Host가 필요합니다.")


def main() -> None:
    config = _config_from_env()
    _validate_runtime_config(config)
    database_path = Path(
        os.environ.get(
            "SEOMYEON_DB_PATH",
            str(Path.home() / ".local/share/seomyeon/seomyeon-web.sqlite3"),
        )
    )
    accounts = AccountStore(database_path)
    seed_email = os.environ.get("SEOMYEON_SEED_EMAIL")
    seed_password = os.environ.get("SEOMYEON_SEED_PASSWORD")
    if seed_email and seed_password:
        if config.production:
            raise RuntimeError("운영 환경에서는 테스트 계정을 시드할 수 없습니다.")
        accounts.ensure_seed_account(seed_email, seed_password)
    law_api_oc = (read_secret("SEOMYEON_LAW_API_OC") or "").strip()
    if config.production and law_api_oc.casefold() == "test":
        raise RuntimeError(
            "운영 환경에서는 법률정보 API 샘플 인증값을 사용할 수 없습니다."
        )
    law_client = LawOpenDataClient(law_api_oc) if law_api_oc else None
    model_backend = backend_from_env()
    print(f"seomyeon: drafting model = {model_backend.name if model_backend else 'deterministic'}")
    app = create_app(
        SqliteStore(database_path), config, accounts, law_client, model_backend
    )
    uvicorn.run(
        app,
        host=os.environ.get("SEOMYEON_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("SEOMYEON_WEB_PORT", "8787")),
    )


if __name__ == "__main__":
    main()
