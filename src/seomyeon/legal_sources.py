from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import utc_now

LAW_API_BASE = "https://www.law.go.kr/DRF"
LAW_PUBLIC_PRECEDENT_URL = "https://www.law.go.kr/LSW/precInfoP.do"
LAW_PUBLIC_STATUTE_URL = "https://www.law.go.kr/LSW/lsInfoP.do"
TAG_RE = re.compile(r"<[^>]+>")
BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


class LegalSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrecedentSummary:
    precedent_id: str
    case_name: str
    case_number: str
    decision_date: str
    court_name: str
    case_type: str
    decision_type: str
    source_name: str
    source_url: str


@dataclass(frozen=True)
class PrecedentDetail(PrecedentSummary):
    issues: str
    holding: str
    referenced_statutes: str
    referenced_cases: str
    retrieved_at: str
    content_hash: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class StatuteSummary:
    mst: str
    law_id: str
    name: str
    law_type: str
    ministry: str
    enforcement_date: str
    source_url: str


@dataclass(frozen=True)
class StatuteArticle(StatuteSummary):
    article_number: str
    article_title: str
    article_text: str
    retrieved_at: str
    content_hash: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _clean_official_text(value: Any, *, max_length: int = 40_000) -> str:
    if not isinstance(value, str):
        return ""
    with_breaks = BREAK_RE.sub("\n", value)
    without_tags = TAG_RE.sub("", with_breaks)
    lines = [
        " ".join(line.split()) for line in html.unescape(without_tags).splitlines()
    ]
    return "\n".join(line for line in lines if line)[:max_length]


class LawOpenDataClient:
    """Small allowlisted client for the official 국가법령정보 공동활용 API."""

    def __init__(
        self,
        oc: str,
        *,
        timeout_seconds: float = 8.0,
        cache_ttl_seconds: int = 600,
        opener: Callable[..., Any] = urlopen,
    ):
        if not oc.strip():
            raise ValueError("국가법령정보 공동활용 API 인증값이 필요합니다.")
        self._oc = oc.strip()
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._opener = opener
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _request(self, endpoint: str, params: dict[str, str | int]) -> dict[str, Any]:
        url = f"{LAW_API_BASE}/{endpoint}?{urlencode({'OC': self._oc, **params})}"
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] < self._cache_ttl_seconds:
                return cached[1]
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Seomyeon/0.1 (+official-law-source-client)",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                if response.status != 200:
                    raise LegalSourceError(
                        f"공식 법률정보 서버가 HTTP {response.status}로 응답했습니다."
                    )
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LegalSourceError(
                "국가법령정보센터 응답을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요."
            ) from exc
        if not isinstance(payload, dict):
            raise LegalSourceError("공식 법률정보 응답 형식이 올바르지 않습니다.")
        with self._lock:
            if len(self._cache) >= 128:
                oldest = min(self._cache, key=lambda key: self._cache[key][0])
                self._cache.pop(oldest, None)
            self._cache[cache_key] = (now, payload)
        return payload

    @staticmethod
    def _summary(item: dict[str, Any]) -> PrecedentSummary:
        precedent_id = str(item.get("판례일련번호", "")).strip()
        if not precedent_id.isdigit():
            raise LegalSourceError("공식 판례 식별자가 누락되었습니다.")
        return PrecedentSummary(
            precedent_id=precedent_id,
            case_name=_clean_official_text(item.get("사건명"), max_length=600),
            case_number=_clean_official_text(item.get("사건번호"), max_length=120),
            decision_date=_clean_official_text(item.get("선고일자"), max_length=30),
            court_name=_clean_official_text(item.get("법원명"), max_length=120),
            case_type=_clean_official_text(item.get("사건종류명"), max_length=80),
            decision_type=_clean_official_text(item.get("판결유형"), max_length=80),
            source_name=_clean_official_text(item.get("데이터출처명"), max_length=80)
            or "국가법령정보센터",
            source_url=f"{LAW_PUBLIC_PRECEDENT_URL}?precSeq={precedent_id}",
        )

    def search_precedents(
        self, query: str, *, display: int = 10
    ) -> list[PrecedentSummary]:
        query = " ".join(query.split())
        if len(query) < 2 or len(query) > 120:
            raise ValueError("판례 검색어는 2자 이상 120자 이하로 입력해 주세요.")
        display = max(1, min(display, 20))
        payload = self._request(
            "lawSearch.do",
            {
                "target": "prec",
                "type": "JSON",
                "mobileYn": "Y",
                "search": 2,
                "query": query,
                "display": display,
                "page": 1,
                "sort": "ddes",
                "datSrcNm": "대법원",
            },
        )
        root = payload.get("PrecSearch", {})
        raw_items = root.get("prec", []) if isinstance(root, dict) else []
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            raise LegalSourceError("공식 판례 검색 응답 형식이 올바르지 않습니다.")
        return [self._summary(item) for item in raw_items if isinstance(item, dict)]

    def get_precedent(self, precedent_id: str) -> PrecedentDetail:
        if not precedent_id.isdigit() or len(precedent_id) > 20:
            raise ValueError("판례 식별자를 확인해 주세요.")
        payload = self._request(
            "lawService.do",
            {
                "target": "prec",
                "type": "JSON",
                "ID": precedent_id,
            },
        )
        item = payload.get("PrecService")
        if not isinstance(item, dict):
            raise LegalSourceError("공식 판례 본문 응답 형식이 올바르지 않습니다.")
        summary = self._summary(
            {
                **item,
                "판례일련번호": item.get("판례정보일련번호") or precedent_id,
                "데이터출처명": "대법원",
            }
        )
        issues = _clean_official_text(item.get("판시사항"))
        holding = _clean_official_text(item.get("판결요지"))
        content = _clean_official_text(item.get("판례내용"), max_length=500_000)
        digest_source = json.dumps(item, ensure_ascii=False, sort_keys=True)
        return PrecedentDetail(
            **asdict(summary),
            issues=issues,
            holding=holding or issues or content[:4000],
            referenced_statutes=_clean_official_text(item.get("참조조문")),
            referenced_cases=_clean_official_text(item.get("참조판례")),
            retrieved_at=utc_now(),
            content_hash=hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _statute_summary(item: dict[str, Any]) -> StatuteSummary:
        mst = str(item.get("법령일련번호", "")).strip()
        law_id = str(item.get("법령ID", "")).strip()
        if not mst.isdigit() or not law_id:
            raise LegalSourceError("공식 법령 식별자가 누락되었습니다.")
        return StatuteSummary(
            mst=mst,
            law_id=law_id,
            name=_clean_official_text(item.get("법령명한글"), max_length=300),
            law_type=_clean_official_text(item.get("법령구분명"), max_length=80),
            ministry=_clean_official_text(item.get("소관부처명"), max_length=120),
            enforcement_date=_clean_official_text(item.get("시행일자"), max_length=20),
            source_url=f"{LAW_PUBLIC_STATUTE_URL}?lsiSeq={mst}",
        )

    def search_statutes(self, query: str, *, display: int = 10) -> list[StatuteSummary]:
        query = " ".join(query.split())
        if len(query) < 2 or len(query) > 120:
            raise ValueError("법령 검색어는 2자 이상 120자 이하로 입력해 주세요.")
        payload = self._request(
            "lawSearch.do",
            {
                "target": "law",
                "type": "JSON",
                "query": query,
                "display": max(1, min(display, 20)),
                "page": 1,
            },
        )
        root = payload.get("LawSearch", {})
        raw_items = root.get("law", []) if isinstance(root, dict) else []
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            raise LegalSourceError("공식 법령 검색 응답 형식이 올바르지 않습니다.")
        results = [
            self._statute_summary(item) for item in raw_items if isinstance(item, dict)
        ]
        return sorted(results, key=lambda item: (item.name != query, len(item.name)))

    def get_statute_article(self, mst: str, article_number: str) -> StatuteArticle:
        if not mst.isdigit() or len(mst) > 20:
            raise ValueError("법령 식별자를 확인해 주세요.")
        normalized_article = re.sub(r"[^0-9의]", "", article_number)
        if not normalized_article or len(normalized_article) > 20:
            raise ValueError("조문 번호를 확인해 주세요.")
        payload = self._request(
            "lawService.do",
            {"target": "law", "type": "JSON", "MST": mst},
        )
        law = payload.get("법령")
        if not isinstance(law, dict):
            raise LegalSourceError("공식 법령 본문 응답 형식이 올바르지 않습니다.")
        basic = law.get("기본정보", {})
        article_root = law.get("조문", {})
        article_items = (
            article_root.get("조문단위", []) if isinstance(article_root, dict) else []
        )
        if isinstance(article_items, dict):
            article_items = [article_items]
        article = next(
            (
                item
                for item in article_items
                if isinstance(item, dict)
                and item.get("조문여부") == "조문"
                and re.sub(r"[^0-9의]", "", str(item.get("조문번호", "")))
                == normalized_article
            ),
            None,
        )
        if article is None:
            raise LegalSourceError(f"제{article_number}조를 찾을 수 없습니다.")
        name = _clean_official_text(basic.get("법령명_한글"), max_length=300)
        digest_source = json.dumps(article, ensure_ascii=False, sort_keys=True)
        return StatuteArticle(
            mst=mst,
            law_id=str(basic.get("법령ID", "")).strip(),
            name=name,
            law_type=_clean_official_text(
                (basic.get("법종구분") or {}).get("content")
                if isinstance(basic.get("법종구분"), dict)
                else basic.get("법종구분"),
                max_length=80,
            ),
            ministry=_clean_official_text(
                (basic.get("소관부처") or {}).get("content")
                if isinstance(basic.get("소관부처"), dict)
                else basic.get("소관부처"),
                max_length=120,
            ),
            enforcement_date=_clean_official_text(basic.get("시행일자"), max_length=20),
            source_url=f"{LAW_PUBLIC_STATUTE_URL}?lsiSeq={mst}",
            article_number=normalized_article,
            article_title=_clean_official_text(article.get("조문제목"), max_length=300),
            article_text=_clean_official_text(
                article.get("조문내용"), max_length=20_000
            ),
            retrieved_at=utc_now(),
            content_hash=hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
        )
