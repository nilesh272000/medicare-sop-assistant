import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.domain.errors import UpstreamUnavailableError
from app.providers.gemini import GeminiChat, GeminiEmbedder
from app.providers.http_retry import post_json


@pytest.fixture
def client(container):
    return TestClient(create_app(container))


def test_ask_ok_shape(client):
    r = client.post("/v1/ask", json={"user_id": "U5001", "question": "What does Code Pink mean?"},
                    headers={"x-correlation-id": "abc-123"})
    assert r.status_code == 200 and r.headers["x-correlation-id"] == "abc-123"
    body = r.json()
    assert body["status"] == "answered" and body["correlation_id"] == "abc-123"
    c = body["citations"][0]
    assert set(c) == {"sop_id", "version", "title", "section", "section_title", "page"}
    assert c["sop_id"] == "SOP-07" and c["version"] == "4.0" and c["section"] == "2"


def test_patient_data_error_format_does_not_echo_input(client):
    q = "Patient Ramesh Kumar, MRN 448812, has a fever after vaccination. What do we do?"
    r = client.post("/v1/ask", json={"user_id": "U2001", "question": q})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "patient_data_detected" and err["correlation_id"].startswith("req-")
    assert "448812" not in r.text and "Ramesh" not in r.text


def test_unknown_user_403(client):
    r = client.post("/v1/ask", json={"user_id": "U0", "question": "What does Code Pink mean?"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "unknown_user"


def test_validation_error_400_without_echo(client):
    r = client.post("/v1/ask", json={"user_id": "U2001", "question": "x" * 2000})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
    assert "xxxx" not in r.text


def test_upstream_failure_maps_to_503(client, container):
    def boom(*a, **k):
        raise UpstreamUnavailableError("llm down", {"retryable": True})
    container.chat.generate = boom
    r = client.post("/v1/ask", json={"user_id": "U5001", "question": "What does Code Pink mean?"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "upstream_unavailable"


def test_documents_filtered_by_role(client):
    fd = {d["sop_id"] for d in client.get("/v1/documents", params={"user_id": "U1001"}).json()}
    hr = {d["sop_id"] for d in client.get("/v1/documents", params={"user_id": "U6001"}).json()}
    assert "SOP-13" not in fd and "SOP-06" not in fd and "SOP-13" in hr and "SOP-06" not in hr


def test_healthz_and_openapi(client):
    assert client.get("/healthz").json()["index_generation"] == 1
    spec = client.get("/openapi.json").json()
    assert "/v1/ask" in spec["paths"] and "422" in spec["paths"]["/v1/ask"]["post"]["responses"]


# ---------- retry policy ----------

def _client(responses):
    seq = iter(responses)

    def handler(request):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retries_5xx_then_succeeds():
    sleeps = []
    c = _client([httpx.Response(503), httpx.Response(429, headers={"retry-after": "1"}),
                 httpx.Response(200, json={"ok": True})])
    assert post_json(c, "https://x/y", json={}, headers={}, timeout_s=1, max_retries=2, op="t",
                     sleep=sleeps.append) == {"ok": True}
    assert len(sleeps) == 2 and sleeps[1] == 1.0          # Retry-After honoured


def test_does_not_retry_4xx():
    sleeps = []
    c = _client([httpx.Response(400), httpx.Response(200, json={})])
    with pytest.raises(UpstreamUnavailableError) as e:
        post_json(c, "https://x/y", json={}, headers={}, timeout_s=1, max_retries=3, op="t", sleep=sleeps.append)
    assert sleeps == [] and e.value.details["retryable"] is False


def test_timeouts_give_up_after_max_retries():
    sleeps = []
    c = _client([httpx.ReadTimeout("t")] * 3)
    with pytest.raises(UpstreamUnavailableError):
        post_json(c, "https://x/y", json={}, headers={}, timeout_s=1, max_retries=2, op="t", sleep=sleeps.append)
    assert len(sleeps) == 2


# ---------- Gemini adapter contract (offline, mocked transport) ----------

def test_gemini_chat_request_and_usage_accounting():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": '{"status":"not_found","answer":"","citations":[]}'}]}}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100}})
    chat = GeminiChat("k", "gemini-2.5-flash", timeout_s=1, max_retries=0, price_in_per_m=0.3,
                      price_out_per_m=2.5, client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = chat.generate("sys", "user", correlation_id="c")
    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent") and seen["key"] == "k"
    assert seen["body"]["generationConfig"]["temperature"] == 0
    assert (res.input_tokens, res.output_tokens) == (1000, 100)
    assert res.cost_usd == pytest.approx(1000 / 1e6 * 0.3 + 100 / 1e6 * 2.5)


def test_gemini_embedder_batches_and_task_types():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"embeddings": [{"values": [0.1] * 4} for _ in body["requests"]]})
    emb = GeminiEmbedder("k", "gemini-embedding-001", timeout_s=1, max_retries=0, price_per_m=0.15, dim=4,
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert emb.embed_documents(["a", "b"], correlation_id="c").shape == (2, 4)
    assert emb.embed_query("q", correlation_id="c").shape == (4,)
    assert bodies[0]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"
    assert bodies[1]["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    assert emb.model_id == "gemini-embedding-001:4"


def test_gemini_requires_key():
    with pytest.raises(ValueError):
        GeminiChat("", "m", timeout_s=1, max_retries=0, price_in_per_m=0, price_out_per_m=0)
