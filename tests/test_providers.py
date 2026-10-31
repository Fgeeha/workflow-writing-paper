import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from workflow_writing_paper.providers import ProviderError, build_pool, extract_json, pick_judges

SCHEMA = {"type": "object", "properties": {"overall": {"type": "number"}, "comments": {"type": "array"}}}


@pytest.mark.parametrize("text, expected", [
    ('{"a": 1}', {"a": 1}),
    ('<think>{"no": 0}</think>Ответ: ```json\n{"a": 2}\n```', {"a": 2}),
    ('Вот оценка: {"a": 3, "b": {"c": [1]}} — готово', {"a": 3, "b": {"c": [1]}}),
    ("совсем не json", None),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_fake_json_retry_and_exhaustion():
    pool = build_pool({"providers": [
        {"id": "ok", "kind": "fake", "bad_json": 2, "score": 8},
        {"id": "bad", "kind": "fake", "bad_json": 3},
    ]})
    comp = pool["ok"].complete([{"role": "user", "content": "x"}], json_schema=SCHEMA)
    assert comp.data["overall"] == 8
    with pytest.raises(ProviderError):
        pool["bad"].complete([{"role": "user", "content": "x"}], json_schema=SCHEMA)


def test_disabled_and_unknown_kind():
    assert build_pool({"providers": [{"id": "x", "kind": "fake", "enabled": False}]}) == {}
    with pytest.raises(ValueError):
        build_pool({"providers": [{"id": "x", "kind": "nope"}]})


def test_pick_judges_excludes_author_family_and_duplicates():
    pool = build_pool({"providers": [
        {"id": "g1", "kind": "fake", "family": "gemma"},
        {"id": "g2", "kind": "fake", "family": "gemma"},
        {"id": "q", "kind": "fake", "family": "qwen"},
        {"id": "y", "kind": "fake", "family": "yandex"},
    ]})
    assert [j.id for j in pick_judges(pool, ["g1", "g2", "q", "y"], "qwen", 2)] == ["g1", "y"]
    assert [j.id for j in pick_judges(pool, ["g1", "g2"], "qwen", 2)] == ["g1"]


def test_generic_cli_json_output_and_cost():
    script = "import json,sys; sys.stdin.read(); print(json.dumps({'out': {'text': 'привет'}, 'u': 10}))"
    pool = build_pool({"providers": [{
        "id": "cli", "kind": "generic-cli", "command": [sys.executable, "-c", script], "stdin": True,
        "output": "json", "text_path": "out.text", "tokens_in_path": "u", "price": {"input_per_1m": 1e6},
    }]})
    comp = pool["cli"].complete([{"role": "user", "content": "x"}])
    assert comp.text == "привет" and comp.tokens_in == 10 and comp.cost_usd == 10.0


def test_openai_adapter_against_stub_server():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            seen["auth"] = self.headers.get("Authorization")
            body = {"choices": [{"message": {"content": '{"overall": 6}'}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        pool = build_pool({"providers": [{
            "id": "o", "kind": "openai", "model": "m", "url": f"http://127.0.0.1:{server.server_port}/v1",
            "api_key_env": "WWP_TEST_KEY", "params": {"top_k": 20}}]})
        import os
        os.environ["WWP_TEST_KEY"] = "k"
        comp = pool["o"].complete([{"role": "user", "content": "x"}], json_schema=SCHEMA)
    finally:
        server.shutdown()
    assert comp.data == {"overall": 6} and comp.tokens_in == 5 and comp.tokens_out == 3
    assert seen["top_k"] == 20 and seen["auth"] == "Bearer k" and seen["model"] == "m"


def test_unclosed_think_is_error():
    pool = build_pool({"providers": [{"id": "t", "kind": "fake", "reply": "<think>рассуждаю и не успеваю"}]})
    with pytest.raises(ProviderError, match="рассуждения"):
        pool["t"].complete([{"role": "user", "content": "x"}])
