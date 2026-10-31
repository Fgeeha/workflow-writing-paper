"""HTTP-адаптеры: ollama, OpenAI-совместимые (включая OpenRouter), GigaChat."""

from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .base import Message, Provider, ProviderError


def _request(url: str, body: dict | bytes | None = None, headers: dict | None = None,
             timeout: float = 600, context: ssl.SSLContext | None = None) -> Any:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ProviderError(f"{url}: HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ProviderError(f"{url}: {e}") from e


class OllamaProvider(Provider):
    kind = "ollama"

    @property
    def url(self) -> str:
        return str(self.cfg.get("url", "http://127.0.0.1:11434")).rstrip("/")

    def _call(self, messages: list[Message], *, json_schema: dict | None, max_tokens: int,
              temperature: float, seed: int | None) -> tuple[str, int, int]:
        options = {"num_predict": max_tokens, "temperature": temperature, "seed": seed,
                   "num_ctx": self.cfg.get("num_ctx"), **(self.cfg.get("params") or {})}
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False,
                                "options": {k: v for k, v in options.items() if v is not None}}
        if json_schema is not None:
            body["format"] = json_schema
        if "think" in self.cfg:
            body["think"] = self.cfg["think"]
        resp = _request(self.url + "/api/chat", body, timeout=self.cfg.get("timeout", 900))
        return resp["message"].get("content", ""), resp.get("prompt_eval_count", 0), resp.get("eval_count", 0)

    def ping(self) -> tuple[bool, str]:
        try:
            names = [m["name"] for m in _request(self.url + "/api/tags", timeout=5)["models"]]
        except ProviderError as e:
            return False, str(e)
        ok = self.model in names
        return ok, "ok" if ok else f"модель {self.model} не загружена в ollama"


class OpenAIProvider(Provider):
    kind = "openai"
    default_url = "http://127.0.0.1:8000/v1"

    @property
    def url(self) -> str:
        return str(self.cfg.get("url", self.default_url)).rstrip("/")

    def headers(self) -> dict[str, str]:
        env = self.cfg.get("api_key_env")
        if not env:
            return {}
        if not os.environ.get(env):
            raise ProviderError(f"{self.id}: переменная {env} не задана (workspace/env)")
        return {"Authorization": f"Bearer {os.environ[env]}"}

    def ssl_context(self) -> ssl.SSLContext | None:
        bundle = self.cfg.get("ca_bundle")
        return ssl.create_default_context(cafile=bundle) if bundle else None

    def _call(self, messages: list[Message], *, json_schema: dict | None, max_tokens: int,
              temperature: float, seed: int | None) -> tuple[str, int, int]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                                "temperature": temperature, **(self.cfg.get("params") or {})}
        if seed is not None:
            body["seed"] = seed
        if json_schema is not None and self.cfg.get("structured"):
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "schema": json_schema}}
        resp = _request(self.url + "/chat/completions", body, self.headers(),
                        timeout=self.cfg.get("timeout", 900), context=self.ssl_context())
        if not resp.get("choices"):
            raise ProviderError(f"{self.id}: пустой ответ: {str(resp)[:300]}")
        usage = resp.get("usage") or {}
        text = resp["choices"][0]["message"].get("content") or ""
        return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

    def ping(self) -> tuple[bool, str]:
        try:
            _request(self.url + "/models", headers=self.headers(), timeout=10, context=self.ssl_context())
        except ProviderError as e:
            return False, str(e)
        return True, "ok"


class OpenRouterProvider(OpenAIProvider):
    kind = "openrouter"
    default_url = "https://openrouter.ai/api/v1"


class GigaChatProvider(OpenAIProvider):
    """GigaChat: OAuth-токен по ключу авторизации, дальше OpenAI-подобный чат.

    Корневой сертификат укажите в `ca_bundle`, ключ — в переменной `api_key_env`
    (по умолчанию GIGACHAT_CREDENTIALS; base64 или client_id:secret).
    """

    kind = "gigachat"
    default_url = "https://gigachat.devices.sberbank.ru/api/v1"
    oauth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._token: tuple[str, float] = ("", 0.0)

    def headers(self) -> dict[str, str]:
        token, expires = self._token
        if time.time() < expires - 60:
            return {"Authorization": f"Bearer {token}"}
        env = self.cfg.get("api_key_env", "GIGACHAT_CREDENTIALS")
        creds = os.environ.get(env)
        if not creds:
            raise ProviderError(f"{self.id}: переменная {env} не задана (workspace/env)")
        if ":" in creds:
            creds = base64.b64encode(creds.encode()).decode()
        form = urllib.parse.urlencode({"scope": self.cfg.get("scope", "GIGACHAT_API_PERS")}).encode()
        resp = _request(self.oauth_url, form, {"Authorization": f"Basic {creds}", "RqUID": str(uuid.uuid4()),
                                               "Content-Type": "application/x-www-form-urlencoded"},
                        timeout=30, context=self.ssl_context())
        self._token = (resp["access_token"], resp["expires_at"] / 1000)
        return {"Authorization": f"Bearer {resp['access_token']}"}
