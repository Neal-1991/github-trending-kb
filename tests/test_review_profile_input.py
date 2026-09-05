import json

import pytest

from scripts import glm_client


def test_hash_uses_exact_prompt_inputs():
    meta = {"description": "demo", "topics": ["ai", "tools"], "stars": 20}
    hashed = glm_client.profile_input_hash("owner/repo", meta, "a" * 6000 + "old")
    assert hashed == glm_client.profile_input_hash(
        "owner/repo", {**meta, "topics": '["ai", "tools"]', "fetched_at": "later"},
        "a" * 6000 + "new",
    )
    assert hashed != glm_client.profile_input_hash("owner/repo", meta, "changed")
    assert hashed != glm_client.profile_input_hash("owner/repo", {**meta, "stars": 21}, "a" * 6000)
    assert hashed != glm_client.profile_input_hash("owner/repo", meta, "a" * 6000, model="other")
    assert glm_client.profile_input_hash("x", {}, "") == glm_client.profile_input_hash(
        "x", {"topics": "[]", "fetched_at": "later"}, ""
    )


def test_request_uses_same_normalized_input(monkeypatch):
    captured = []
    valid = {field: "value" for field in glm_client.PROFILE_FIELDS}

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(valid)}}]}

    def post(*args, **kwargs):
        captured.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(glm_client, "GLM_API_KEY", "offline")
    monkeypatch.setattr(glm_client.requests, "post", post)
    meta = {"topics": '["ai", "tools"]'}
    assert glm_client.profile_repo("x", meta, "a" * 6000 + "TRUNCATED") == valid
    prompt = captured[0]["messages"][1]["content"]
    assert prompt == glm_client._user_content("x", {"topics": ["ai", "tools"]}, "a" * 6000)
    assert "Topics: ai, tools" in prompt
    assert "TRUNCATED" not in prompt


@pytest.mark.parametrize("body", [
    None, [], {}, {"choices": []}, {"choices": {}}, {"choices": [None]},
    {"choices": [{"message": None}]}, {"choices": [{"message": []}]},
    {"choices": [{"message": {"content": ["invalid"]}}]},
])
def test_malformed_success_retries_without_crashing(monkeypatch, body):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return body

    def post(*args, **kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(glm_client, "GLM_API_KEY", "offline")
    monkeypatch.setattr(glm_client.requests, "post", post)
    monkeypatch.setattr(glm_client.time, "sleep", lambda _: None)
    assert glm_client.profile_repo("x", {}, "readme") is None
    assert len(calls) == 3
