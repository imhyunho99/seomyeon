"""Model gateway: one JSON-in/JSON-out call shape over local and hosted models.

Providers (``SEOMYEON_MODEL_PROVIDER``):

- unset/``none``: no model; the deterministic scaffold is the draft.
- ``ollama``: a local model on this machine. The URL must be loopback so client
  material never leaves the host by misconfiguration.
- ``anthropic``: Claude via the official SDK. Sends client material to an
  external service, so it also requires ``SEOMYEON_ALLOW_EXTERNAL_MODEL=1``.

Secrets are read from the environment first, then from ``SECRETS_FILE``
(``KEY=VALUE`` lines, never committed).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SECRETS_FILE = str(Path.home() / ".config/seomyeon/secrets.env")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "exaone3.5:7.8b"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ModelConfigError(RuntimeError):
    pass


class ModelError(RuntimeError):
    pass


class ModelBackend(Protocol):
    name: str

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


def read_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = Path(SECRETS_FILE)
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, raw = line.partition("=")
        if sep and key.strip() == name:
            return raw.strip().strip('"').strip("'") or None
    return None


@dataclass
class FakeBackend:
    """Deterministic backend for tests."""

    respond: Callable[[str, str, dict[str, Any]], dict[str, Any]]
    name: str = "fake"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def complete_json(self, system, user, schema):
        self.calls.append((system, user))
        return self.respond(system, user, schema)


@dataclass
class OllamaBackend:
    model: str = DEFAULT_OLLAMA_MODEL
    base_url: str = DEFAULT_OLLAMA_URL
    timeout: float = 300.0

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    def complete_json(self, system, user, schema):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
        request = Request(
            f"{self.base_url.rstrip('/')}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            raise ModelError(f"로컬 모델 호출에 실패했습니다: {exc}") from exc
        try:
            return json.loads(body["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ModelError("로컬 모델이 JSON 형식으로 답하지 않았습니다.") from exc


@dataclass
class ClaudeBackend:
    api_key: str
    model: str = DEFAULT_CLAUDE_MODEL
    effort: str = "medium"
    _client: Any = None

    @property
    def name(self) -> str:
        return f"anthropic:{self.model}"

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ModelConfigError(
                    "anthropic 패키지가 없습니다: pip install -e '.[claude]'"
                ) from exc
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def complete_json(self, system, user, schema):
        import anthropic

        client = self._get_client()
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                # 안전 분류기가 거절하면 서버가 다른 모델로 이어서 처리한다
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except anthropic.RateLimitError as exc:
            raise ModelError("Claude 호출 한도에 걸렸습니다. 잠시 후 다시 시도하세요.") from exc
        except anthropic.APIStatusError as exc:
            raise ModelError(f"Claude 호출 실패 ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ModelError("Claude에 연결할 수 없습니다.") from exc
        if response.stop_reason == "refusal":
            raise ModelError("Claude가 이 요청을 거절했습니다.")
        if response.stop_reason == "max_tokens":
            raise ModelError("Claude 응답이 길이 한도에서 잘렸습니다.")
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError("Claude 응답을 JSON으로 읽을 수 없습니다.") from exc


def backend_from_env() -> ModelBackend | None:
    provider = os.environ.get("SEOMYEON_MODEL_PROVIDER", "none").strip().casefold()
    if provider in {"", "none"}:
        return None
    if provider == "ollama":
        url = os.environ.get("SEOMYEON_OLLAMA_URL", DEFAULT_OLLAMA_URL)
        if urlparse(url).hostname not in LOOPBACK_HOSTS:
            raise ModelConfigError(
                "로컬 모델 주소는 이 컴퓨터(127.0.0.1/localhost)여야 합니다."
            )
        return OllamaBackend(
            model=os.environ.get("SEOMYEON_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
            base_url=url,
        )
    if provider == "anthropic":
        if os.environ.get("SEOMYEON_ALLOW_EXTERNAL_MODEL") != "1":
            raise ModelConfigError(
                "외부 모델로 사건 자료를 보내려면 SEOMYEON_ALLOW_EXTERNAL_MODEL=1을 "
                "명시해야 합니다(의뢰인 동의와 데이터 보존 조건 확인 후)."
            )
        api_key = read_secret("ANTHROPIC_API_KEY")
        if not api_key:
            raise ModelConfigError(
                f"ANTHROPIC_API_KEY가 없습니다. 환경변수나 {SECRETS_FILE}에 넣어 주세요."
            )
        return ClaudeBackend(
            api_key=api_key,
            model=os.environ.get("SEOMYEON_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
            effort=os.environ.get("SEOMYEON_CLAUDE_EFFORT", "medium"),
        )
    raise ModelConfigError(f"알 수 없는 모델 제공자입니다: {provider}")
