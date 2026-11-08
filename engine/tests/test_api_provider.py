"""The Anthropic API provider: provider choice, structured output, error mapping, the API
key's storage, and batch tagging. Every client here is a fake - nothing touches the network."""

import json
import sys
import time
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from qbx import api_key, batch_tagging, llm_api
from qbx import llm as llm_mod
from qbx.llm_api import AnthropicLLM
from qbx.schema import Paper, Question, QuestionPart
from qbx.topics import Topic

TOPICS = [
    Topic(id="4", name="Electrochemistry"),
    Topic(id="4.1", name="Electrolysis", parent="4"),
    Topic(id="7", name="Chemical reactions"),
    Topic(id="7.2", name="Rate of reaction", parent="7"),
]


class Answer(BaseModel):
    ok: bool
    note: str = ""


# -- fakes -------------------------------------------------------------------------------


class MemoryKeyring:
    def __init__(self):
        self.saved: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.saved.get((service, name))

    def set_password(self, service, name, value):
        self.saved[(service, name)] = value

    def delete_password(self, service, name):
        self.saved.pop((service, name), None)


def _message(payload, stop_reason="end_turn", request_id="req_test"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, stop_details=None, _request_id=request_id, id="msg_1",
    )


def _status_error(cls, status, error_type, headers=None, message="nope", error_code=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = {"type": error_type, "message": message}
    if error_code:
        error["details"] = {"error_code": error_code}
    body = {"type": "error", "error": error, "request_id": "req_fail"}
    response = httpx2.Response(status, headers={"request-id": "req_fail", **(headers or {})}, json=body, request=request)
    return cls(f"Error code: {status}", response=response, body=body)


class FakeMessages:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.reply(kwargs) if callable(self.reply) else self.reply
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeBatches:
    """A Message Batches API that ends a batch after `polls_to_end` retrieves, and returns
    its results in reverse order. `outcome(custom_id, params, batch_number)` picks each
    result: "ok", "errored", "invalid" or "expired"."""

    def __init__(self, answer, outcome=lambda cid, params, n: "ok", polls_to_end=1):
        self.answer = answer
        self.outcome = outcome
        self.polls_to_end = polls_to_end
        self.created: list[list[dict]] = []
        self._polls: dict[str, int] = {}

    def create(self, requests):
        requests = list(requests)
        self.created.append(requests)
        batch_id = f"msgbatch_{len(self.created)}"
        self._polls[batch_id] = 0
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    def retrieve(self, batch_id):
        self._polls[batch_id] = self._polls.get(batch_id, 0) + 1
        n = int(batch_id.split("_")[1])
        ended = self._polls[batch_id] >= self.polls_to_end
        total = len(self.created[n - 1])
        return SimpleNamespace(
            id=batch_id, processing_status="ended" if ended else "in_progress",
            request_counts=SimpleNamespace(processing=0 if ended else total),
        )

    def results(self, batch_id):
        n = int(batch_id.split("_")[1])
        out = []
        for request in self.created[n - 1]:
            cid, params = request["custom_id"], request["params"]
            kind = self.outcome(cid, params, n)
            if kind == "ok":
                result = SimpleNamespace(type="succeeded", message=_message(self.answer(params["messages"][0]["content"])))
            elif kind in ("errored", "invalid"):
                etype = "invalid_request_error" if kind == "invalid" else "api_error"
                result = SimpleNamespace(type="errored", error=SimpleNamespace(type="error", error=SimpleNamespace(type=etype)))
            else:
                result = SimpleNamespace(type=kind)
            out.append(SimpleNamespace(custom_id=cid, result=result))
        return list(reversed(out))  # any order: matched by custom_id


class FakeClient:
    def __init__(self, reply=None, batches=None):
        self.messages = FakeMessages(reply)
        self.messages.batches = batches
        self.beta = SimpleNamespace(messages=FakeMessages(reply))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = MemoryKeyring()
    api_key.set_backend(store)
    yield store
    api_key.set_backend(None)
    llm_api.set_client_factory(None)
    llm_mod.set_llm_override(None)


# -- provider choice ---------------------------------------------------------------------


def test_a_fresh_install_and_an_old_settings_file_use_claude_code(tmp_path):
    assert llm_mod.load_settings().provider == "claude_code"  # no API keys in the app
    llm_mod.save_settings(llm_mod.Settings(enabled=True))
    assert isinstance(llm_mod.get_llm("tag"), llm_mod.ClaudeLLM)

    # a settings.json written before providers existed: no "provider" key at all
    (llm_mod.CONFIG_DIR / "settings.json").write_text(json.dumps({"enabled": True, "tag_model": "haiku"}), encoding="utf-8")
    settings = llm_mod.load_settings()
    assert settings.provider == "claude_code" and settings.tag_model == "claude-haiku-5-5"
    assert isinstance(llm_mod.get_llm("tag"), llm_mod.ClaudeLLM)
    assert isinstance(llm_mod.get_llm("request"), llm_mod.ClaudeLLM)

    llm_mod.save_settings(settings.model_copy(update={"provider": "api"}))
    assert llm_mod.load_settings().provider == "api"
    fix = llm_mod.get_llm("fix")
    assert isinstance(fix, AnthropicLLM) and fix.model == "claude-sonnet-5-5" and fix.think


def test_thinking_and_effort_per_job():
    tag = AnthropicLLM("claude-haiku-5-5", think=False).params("p", Answer)
    assert "thinking" not in tag and tag["output_config"]["effort"] == "low"  # never "disabled"
    assert tag["output_config"]["format"]["type"] == "json_schema"
    assert tag["output_config"]["format"]["schema"]["additionalProperties"] is False
    request = AnthropicLLM("claude-sonnet-5-5", think=True).params("p", Answer)
    assert request["thinking"] == {"type": "adaptive"} and request["output_config"]["effort"] == "high"
    assert "fallbacks" not in request  # batch-safe: the Batches API rejects fallbacks
    haiku_thinking = AnthropicLLM("claude-haiku-5-5", think=True).params("p", Answer)
    assert haiku_thinking["output_config"]["effort"] == "medium"


# -- structured output -------------------------------------------------------------------


def test_structured_output_is_parsed_into_the_schema():
    client = FakeClient(_message({"ok": True, "note": "fine"}))
    got = AnthropicLLM("claude-haiku-5-5", think=False, client=client).json("Say ok", Answer)
    assert got == Answer(ok=True, note="fine")
    sent = client.messages.calls[0]
    assert sent["model"] == "claude-haiku-5-5" and sent["messages"] == [{"role": "user", "content": "Say ok"}]
    assert client.beta.messages.calls == []  # Haiku has no server-side fallback


def test_sonnet_calls_opt_into_the_refusal_fallback():
    client = FakeClient(_message({"ok": True}))
    AnthropicLLM("claude-sonnet-5-5", client=client).json("x", Answer)
    sent = client.beta.messages.calls[0]
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]


def test_a_reply_that_does_not_fit_is_asked_again():
    replies = iter([_message("not json"), _message({"ok": False})])
    client = FakeClient(lambda kw: next(replies))
    assert AnthropicLLM("claude-haiku-5-5", client=client).json("x", Answer).ok is False
    assert len(client.messages.calls) == 2


# -- errors ------------------------------------------------------------------------------


def _fails_with(error):
    llm = AnthropicLLM("claude-haiku-5-5", client=FakeClient(error))
    with pytest.raises(llm_mod.LLMError) as caught:
        llm.json("x", Answer)
    return caught.value


def test_rate_limit_pauses_with_a_reset_time_from_retry_after():
    before = time.time()
    error = _fails_with(_status_error(anthropic.RateLimitError, 429, "rate_limit_error", {"retry-after": "120"}))
    assert isinstance(error, llm_mod.UsageLimitError)
    assert before + 119 <= error.resets_at <= time.time() + 121
    assert "req_fail" in str(error)


def test_overloaded_pauses_too():
    error = _fails_with(_status_error(anthropic.OverloadedError, 529, "overloaded_error"))
    assert isinstance(error, llm_mod.UsageLimitError) and error.resets_at > time.time()


def test_a_rejected_key_says_to_check_settings():
    error = _fails_with(_status_error(anthropic.AuthenticationError, 401, "authentication_error"))
    assert not isinstance(error, llm_mod.UsageLimitError)
    assert "The API key was rejected - check it in Settings" in str(error)
    assert "isn't allowed" in str(_fails_with(_status_error(anthropic.PermissionDeniedError, 403, "permission_error")))


def test_billing_problems_say_so():
    for error in (
        _status_error(anthropic.APIStatusError, 402, "billing_error"),
        _status_error(anthropic.BadRequestError, 400, "invalid_request_error",
                      message="Your credit balance is too low to access the Anthropic API."),
    ):
        assert "out of credit" in str(_fails_with(error))
    spend = _status_error(anthropic.EnforcedSpendLimitReachedError, 429, "rate_limit_error",
                          error_code="enforced_spend_limit_reached")
    got = _fails_with(spend)
    assert "spend limit" in str(got) and not isinstance(got, llm_mod.UsageLimitError)


def test_a_refusal_is_an_error():
    refused = _message("", stop_reason="refusal")
    refused.stop_details = SimpleNamespace(category="cyber")
    error = _fails_with(refused)
    assert "declined" in str(error) and "cyber" in str(error)


def test_failures_log_their_request_id(caplog):
    with caplog.at_level("WARNING", logger="qbx.llm"):
        _fails_with(_status_error(anthropic.AuthenticationError, 401, "authentication_error"))
    assert "req_fail" in caplog.text


# -- the API key -------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    sys.modules.pop("main", None)
    import main

    monkeypatch.setattr(batch_tagging, "POLL_SECONDS", 0.01)
    return main


def test_the_key_lives_in_the_credential_store_and_never_reaches_the_ui(backend, isolated):
    client = TestClient(backend.app)
    secret = "sk-ant-api03-SECRETSECRET-abcd"
    assert client.get("/api/settings").json()["api_key"] == {"set": False, "source": None, "last4": None}

    status = client.put("/api/settings/api_key", json={"key": secret}).json()
    assert status == {"set": True, "source": "saved", "last4": "abcd"}
    assert isolated.saved[("ClassarkExamBuilder", "anthropic_api_key")] == secret
    client.put("/api/settings", json={"enabled": True, "provider": "api", "api_key": secret})  # ignored
    settings = client.get("/api/settings")
    assert secret not in settings.text and settings.json()["provider"] == "api"
    assert secret not in (llm_mod.CONFIG_DIR / "settings.json").read_text(encoding="utf-8")

    # Test connection: one tiny call, through the key
    made = []

    def factory():
        made.append(api_key.get_api_key())
        return FakeClient(_message({"ok": True}))

    llm_api.set_client_factory(factory)
    checked = client.post("/api/settings/check")
    assert checked.status_code == 200 and checked.json()["ok"] is True and made == [secret]
    assert secret not in checked.text

    llm_api.set_client_factory(lambda: FakeClient(_status_error(anthropic.AuthenticationError, 401, "authentication_error")))
    bad = client.post("/api/settings/check")
    assert bad.status_code == 502 and "rejected" in bad.json()["detail"] and secret not in bad.text

    assert client.delete("/api/settings/api_key").json()["set"] is False
    assert isolated.saved == {}


def test_the_environment_variable_overrides_the_saved_key(monkeypatch):
    api_key.set_api_key("sk-ant-saved-1111")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-2222")
    assert api_key.get_api_key() == "sk-ant-from-env-2222"
    assert api_key.api_key_status() == {"set": True, "source": "environment", "last4": "2222"}


def test_no_key_says_where_to_add_one():
    with pytest.raises(llm_mod.LLMError, match="add one in Settings"):
        llm_api.make_client()


# -- batch tagging -----------------------------------------------------------------------


def _paper(key: str, subject_marks: list[tuple[int, str]]) -> Paper:
    questions = [
        Question(
            paper_key=key, board="Cambridge", subject="0620", question_number=n, stem_text="",
            stem_image_path="", total_marks=2,
            parts=[QuestionPart(id=f"{n}.a", question_id=str(n), path="a", depth=1, text=f"{n} (a) {text} [2]", image_path="", marks=2)],
        )
        for n, text in subject_marks
    ]
    return Paper(paper_key=key, board="Cambridge", level="IGCSE", subject="0620", paper="4", variant="1",
                 session="M/J", year=24, questions=questions)


def _answer(prompt: str) -> dict:
    """Tags every question the prompt lists: electrolysis or rates, by its wording."""
    import re

    questions = []
    for number, body in re.findall(r"=== Question (\d+) ===(.*?)(?==== Question|\Z)", prompt, re.S):
        topic = "4.1" if "electrolys" in body.lower() else "7.2"
        questions.append({"question_number": int(number), "parts": [{"path": "a", "topic_ids": [topic], "depends_on": []}]})
    return {"questions": questions}


def _bank(tmp_path):
    out = tmp_path / "out"
    for key, questions in {
        "P1": [(1, "Name the product of electrolysis."), (2, "Explain how temperature affects rate.")],
        "P2": [(1, "Describe the electrolysis of brine.")],
    }.items():
        (out / key).mkdir(parents=True)
        (out / key / "paper.json").write_text(_paper(key, questions).model_dump_json(), encoding="utf-8")


def _wait_until(client, done):
    for _ in range(500):
        state = client.get("/api/state").json()["tagging"]
        if done(state):
            return state
        time.sleep(0.01)
    raise AssertionError(f"tagging never got there: {state}")


def _api_settings(client):
    client.put("/api/settings", json={"enabled": True, "provider": "api"})
    client.put("/api/topics/0620", json={"topics": [t.model_dump() for t in TOPICS]})


def test_batch_tagging_end_to_end_with_a_retry(backend, tmp_path):
    _bank(tmp_path)
    # P2's request fails in the first batch and goes into a second one
    batches = FakeBatches(_answer, outcome=lambda cid, params, n: "errored" if n == 1 and "brine" in params["messages"][0]["content"] else "ok")
    client_api = FakeClient(_message({"ok": True}), batches)
    llm_api.set_client_factory(lambda: client_api)
    client = TestClient(backend.app)
    _api_settings(client)

    client.post("/api/tagging/start")
    state = _wait_until(client, lambda s: not s["running"])
    assert state["failed"] == [] and state["done"] == 3 and state["stage"] == "Finished"

    assert [len(b) for b in batches.created] == [2, 1]  # every paper at once, then the retry
    first = batches.created[0][0]["params"]
    assert first["model"] == "claude-haiku-5-5" and "fallbacks" not in first and "thinking" not in first
    assert client_api.messages.calls == []  # no direct calls: everything went by batch
    assert batch_tagging.pending() is None

    p1 = client.get("/api/papers/P1/questions/1").json()["parts"][0]["tags"]
    p1q2 = client.get("/api/papers/P1/questions/2").json()["parts"][0]["tags"]
    p2 = client.get("/api/papers/P2/questions/1").json()["parts"][0]["tags"]
    assert p1["topic_ids"] == ["4.1"] and p1q2["topic_ids"] == ["7.2"] and p2["topic_ids"] == ["4.1"]

    # tagged now, so nothing is sent again
    client.post("/api/tagging/start")
    _wait_until(client, lambda s: not s["running"])
    assert len(batches.created) == 2


def test_a_restart_resumes_the_saved_batch(backend, tmp_path, monkeypatch):
    _bank(tmp_path)
    batches = FakeBatches(_answer, polls_to_end=10**9)  # still running at Anthropic
    llm_api.set_client_factory(lambda: FakeClient(None, batches))
    client = TestClient(backend.app)
    _api_settings(client)

    client.post("/api/tagging/start")
    _wait_until(client, lambda s: "Waiting for Claude's batch" in (s.get("stage") or ""))
    client.post("/api/tagging/stop")
    state = _wait_until(client, lambda s: not s["running"])
    assert state["stage"] == "Paused" and "still running" in state["paused"]
    saved = json.loads((llm_mod.CONFIG_DIR / "batches.json").read_text(encoding="utf-8"))
    assert saved["tagging"]["batch_id"] == "msgbatch_1"
    assert {r["paper_key"] for r in saved["tagging"]["requests"].values()} == {"P1", "P2"}

    # the app restarts; meanwhile the batch finished
    batches.polls_to_end = 1
    sys.modules.pop("main", None)
    import main as restarted

    client = TestClient(restarted.app)
    client.get("/api/state")  # the first poll picks the batch back up
    state = _wait_until(client, lambda s: s.get("running") is False and s.get("stage") == "Finished")
    assert state["done"] == 3 and state["failed"] == []
    assert len(batches.created) == 1  # collected, not paid for twice
    assert batch_tagging.pending() is None
    assert client.get("/api/papers/P2/questions/1").json()["parts"][0]["tags"]["topic_ids"] == ["4.1"]


def test_requests_a_batch_keeps_failing_end_as_direct_calls(backend, tmp_path):
    _bank(tmp_path)
    # P1 always expires; P2's request is invalid - a batch would only refuse it again
    batches = FakeBatches(_answer, outcome=lambda cid, params, n: "expired" if "temperature" in params["messages"][0]["content"] else "invalid")
    direct = FakeClient(lambda kw: _message(_answer(kw["messages"][0]["content"])), batches)
    llm_api.set_client_factory(lambda: direct)
    client = TestClient(backend.app)
    _api_settings(client)

    client.post("/api/tagging/start")
    state = _wait_until(client, lambda s: not s["running"])
    assert state["failed"] == [] and state["done"] == 3
    assert [len(b) for b in batches.created] == [2, 1]  # P1 retried once by batch; P2 never
    assert len(direct.messages.calls) == 2  # then each directly


def test_rate_limited_batch_submission_pauses(backend, tmp_path):
    _bank(tmp_path)

    class Limited(FakeBatches):
        def create(self, requests):
            raise _status_error(anthropic.RateLimitError, 429, "rate_limit_error", {"retry-after": "30"})

    llm_api.set_client_factory(lambda: FakeClient(None, Limited(_answer)))
    client = TestClient(backend.app)
    _api_settings(client)
    client.post("/api/tagging/start")
    state = _wait_until(client, lambda s: not s["running"])
    assert state["stage"] == "Paused" and "rate limit" in state["paused"] and state["resets_at"] > time.time()
