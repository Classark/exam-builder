"""Claude through the Messages API (the official `anthropic` SDK): the Classark server with
the teacher's Classark account (`classark=True`, production), or the Anthropic API with an
API key (development only).

Same `LLM` protocol as `ClaudeLLM` (`json(prompt, schema)`), so
tagging, requests, chat and adjusting don't know which one they got. Every call is
structured output (`output_config.format` with the pydantic model's JSON schema), so a
reply is valid JSON for the schema or an error.

Thinking and effort, per model (the API's rules, not Claude Code's):
- `think=False` (tagging, a whole bank of papers): no `thinking` parameter, effort "low".
  Haiku 5.5, Sonnet 5.5 and Opus 5.5 all think adaptively by default and refuse
  `thinking: {"type": "disabled"}` in some combination (Sonnet 5.5 and Opus 5.5 always), so
  effort is the lever: at "low" the model thinks little or not at all, which keeps output
  near the ~1,300 tokens a paper's tags need.
- `think=True` (requests, chat, adjusting): `thinking: {"type": "adaptive"}` at the model's
  `THINK_EFFORT` ("high" for Sonnet and Opus, Haiku's default "medium").
Sonnet 5.5 and Opus 5.5 direct calls opt into the server-side refusal fallback
(`fallbacks: "default"`); Haiku has none, and the Batches API rejects the parameter.

Errors become the app's own (`map_error`): a 429 or overload, after the SDK's own retries,
is `UsageLimitError` with `resets_at` from `retry-after`, so background runs pause as they
do on a subscription's limit; a rejected key, a billing problem and a refusal are
`LLMError`s that say what to do. Each failure's `request_id` is logged.

Batches (`batch_params`, `parse_message`) are used by batch_tagging.py.

The Classark server (the platform repo's EXAM_BUILDER_API.md) speaks the Messages API with
four differences the client handles: the bearer is the account's 15-minute access token
(renewed by account.access_token, and once more after a 401 token_expired - `call`); every
call names its job (X-Classark-Job) and tag calls their paper for the shared cache
(X-Classark-Cache); no beta header but the refusal fallback's - so no SDK `credentials=`
provider, which adds the OAuth beta; and its errors carry a `classark_code` (out of credit,
no subscription, signed out), mapped to the teacher's own words.
"""

import json
import logging
import time
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from qbx import api_key
from qbx import llm as llm_mod
from qbx.llm import LLMError, UsageLimitError

log = logging.getLogger("qbx.llm")

T = TypeVar("T", bound=BaseModel)

SYSTEM = "You help a teacher organise past exam papers. Answer only from what you are given."

# effort with thinking on; models not listed get "high"
THINK_EFFORT = {"claude-haiku-5-5": "medium", "claude-sonnet-5-5": "high", "claude-opus-5-5": "high"}
QUICK_EFFORT = "low"
# models with a server-side refusal fallback ("default" routes by refusal category)
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# thinking counts toward max_tokens; both stay under the SDK's non-streaming ceiling
MAX_TOKENS_QUICK = 16_000
MAX_TOKENS_THINK = 20_000
# the SDK retries 408/409/429/5xx and dropped connections itself, with backoff
SDK_RETRIES = 3
DEFAULT_WAIT = 60.0
# the SDK's own retries against the Classark server: its default, and the contract's advice
CLASSARK_RETRIES = 2

# tests swap this for a fake: a callable returning a client with the SDK's shape
_client_factory: Callable[[], object] | None = None


def set_client_factory(factory: Callable[[], object] | None) -> None:
    global _client_factory
    _client_factory = factory


def make_client(job: str = "tag", classark: bool = False, token: str | None = None):
    """An SDK client: for the Classark server with an account access token, or for the
    Anthropic API with the saved (or environment) API key."""
    if _client_factory is not None:
        return _client_factory()
    import anthropic

    if classark:
        from qbx import account, legal

        # an explicit auth_token keeps the SDK from reading ANTHROPIC_API_KEY, which the
        # server would refuse (401 api_key_not_accepted)
        return anthropic.Anthropic(
            base_url=account.API_BASE,
            auth_token=token or account.access_token(),
            max_retries=CLASSARK_RETRIES,
            # anthropic-version is the SDK's own (2026-10-21 from SDK 2.0), which the server
            # allows since it was added there; it refused it before ("not supported")
            default_headers={"X-Classark-Job": job, "X-Classark-Client": f"exam-builder/{legal.version()}"},
        )
    key = api_key.get_api_key()
    if not key:
        raise LLMError("No Anthropic API key is saved - add one in Settings.")
    return anthropic.Anthropic(api_key=key, max_retries=SDK_RETRIES)


def output_schema(schema: type[BaseModel]) -> dict:
    """The pydantic model's JSON schema in the form structured outputs accept."""
    from anthropic import transform_schema

    return transform_schema(schema.model_json_schema())


def _when(resets_at: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(resets_at).strftime("%H:%M")


def _wait_seconds(error) -> float:
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value:
            try:
                return max(0.0, float(value) * scale)
            except ValueError:
                pass
    return DEFAULT_WAIT


def classark_code(error) -> str | None:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        code = inner.get("classark_code")
        return code if isinstance(code, str) else None
    return None


def _classark_error(error, code: str | None, suffix: str) -> Exception | None:
    """A Classark server error in the teacher's words; None for anything else (the caller
    passes it on as it is)."""
    import anthropic

    from qbx import account

    if code == "out_of_credit":
        # pauses a tagging run, like a usage limit, rather than failing every paper
        return UsageLimitError("You're out of Claude credit. Add credit from the account menu, "
                               "or wait for your monthly allowance to refill." + suffix)
    if code == "no_subscription":
        return LLMError("Your Classark Exam Builder subscription isn't active - renew it from the account menu." + suffix)
    if code in ("session_revoked", "token_invalid", "token_missing", "token_expired"):
        account.forget_sign_in()
        return LLMError("Your Classark sign-in has ended - please sign in again." + suffix)
    if code == "account_disabled":
        return LLMError("This Classark account is disabled." + suffix)
    if code == "proxy_disabled":
        return LLMError("Claude is switched off on Classark's side for now - try again later." + suffix)
    if code in ("user_rate_limit", "user_concurrency"):
        resets = time.time() + _wait_seconds(error)
        return UsageLimitError(f"Too many Claude requests at once. Try again at {_when(resets)}." + suffix, resets)
    if code == "upstream_auth":
        return LLMError("Classark couldn't reach Claude just now - try again in a minute." + suffix)
    if code:
        message = getattr(error, "api_message", None) or str(error)
        return LLMError(f"Classark refused the request ({code}): {message}" + suffix)
    # no classark_code: Anthropic's own reply, passed on unchanged
    if isinstance(error, anthropic.APIConnectionError):
        return LLMError("Couldn't reach Classark - check the internet connection.")
    if isinstance(error, (anthropic.RateLimitError, anthropic.OverloadedError)):
        resets = time.time() + _wait_seconds(error)
        return UsageLimitError(f"Claude is busy right now. Try again at {_when(resets)}." + suffix, resets)
    if isinstance(error, anthropic.APIStatusError):
        return LLMError(f"Claude failed ({error.status_code}): {error.api_message or error}" + suffix)
    return None


def map_error(error: Exception, classark: bool = False) -> Exception:
    """An SDK exception as the app's own error, most specific first."""
    import anthropic

    request_id = getattr(error, "request_id", None)
    suffix = f" (request {request_id})" if request_id else ""
    if classark:
        log.warning("Classark error %s %s, request_id=%s", getattr(error, "status_code", None),
                    classark_code(error), request_id)
        return _classark_error(error, classark_code(error), suffix) or error
    if isinstance(error, anthropic.APIStatusError):
        log.warning("Anthropic API error %s %s, request_id=%s: %s",
                    error.status_code, error.type, request_id, error.api_message or error)
    elif isinstance(error, anthropic.APIError):
        log.warning("Anthropic API call failed: %s", error)

    billing = "Your Anthropic account is out of credit or has a billing problem - check Plans & Billing in the Anthropic Console."
    if isinstance(error, anthropic.EnforcedSpendLimitReachedError):
        return LLMError("Your Anthropic account's spend limit is reached - raise it in the Anthropic Console." + suffix)
    if isinstance(error, anthropic.RateLimitError):
        resets = time.time() + _wait_seconds(error)
        e = UsageLimitError(f"The Anthropic API's rate limit is reached. Try again at {_when(resets)}." + suffix, resets)
        return e
    if isinstance(error, anthropic.OverloadedError):
        resets = time.time() + _wait_seconds(error)
        return UsageLimitError(f"Claude is overloaded right now. Try again at {_when(resets)}." + suffix, resets)
    if isinstance(error, anthropic.APIStatusError) and (error.status_code == 402 or error.type == "billing_error"):
        return LLMError(billing + suffix)
    if isinstance(error, anthropic.AuthenticationError):
        return LLMError("The API key was rejected - check it in Settings." + suffix)
    if isinstance(error, anthropic.PermissionDeniedError):
        if error.error_code == "billing_frozen":
            return LLMError(billing + suffix)
        return LLMError("The API key isn't allowed to do this - check it in Settings." + suffix)
    if isinstance(error, anthropic.NotFoundError):
        return LLMError("That Claude model isn't available to this API key - pick another in Settings." + suffix)
    if isinstance(error, anthropic.BadRequestError):
        message = error.api_message or str(error)
        # the out-of-credit reply has been a plain 400 invalid_request_error, not a 402
        if error.error_code == "organization_on_hold" or "credit balance" in message.lower():
            return LLMError(billing + suffix)
        return LLMError(f"The Anthropic API refused the request: {message}" + suffix)
    if isinstance(error, anthropic.APIStatusError):
        return LLMError(f"The Anthropic API failed ({error.status_code}): {error.api_message or error}" + suffix)
    if isinstance(error, anthropic.APIConnectionError):
        return LLMError("Couldn't reach the Anthropic API - check the internet connection.")
    return error


def parse_message(message, schema: type[T]) -> T:
    """A Messages API reply as the schema's model. A refusal or a cut-off reply is an
    LLMError; JSON that doesn't fit is a ValueError (worth another try)."""
    request_id = getattr(message, "_request_id", None) or getattr(message, "id", None)
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        log.warning("Claude declined, request_id=%s category=%s", request_id, category)
        raise LLMError("Claude declined to answer this one" + (f" ({category})" if category else "") + ".")
    if stop == "max_tokens":
        log.warning("Claude's reply was cut off at max_tokens, request_id=%s", request_id)
        raise LLMError("Claude's reply was too long and was cut off.")
    text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
    if not text.strip():
        raise ValueError("Claude gave no answer.")
    return schema.model_validate(json.loads(text))


class AnthropicLLM:
    """The LLM protocol over the Anthropic API."""

    RETRIES = 2  # for a reply that doesn't fit the schema; the SDK retries HTTP failures
    supports_batches = True

    def __init__(self, model: str, think: bool = True, client=None, job: str = "tag", classark: bool = False):
        self.model = model
        self.think = think
        self.job = job
        self.classark = classark
        self._client = client
        self._token: str | None = None

    @property
    def client(self):
        """The SDK client; on Classark, rebuilt whenever the access token was renewed."""
        if self.classark and _client_factory is None:
            from qbx import account

            try:
                token = account.access_token()
            except account.AccountError as e:
                raise LLMError(str(e)) from e
            if self._client is None or token != self._token:
                self._client, self._token = make_client(self.job, classark=True, token=token), token
            return self._client
        if self._client is None:
            self._client = make_client(self.job, self.classark)
        return self._client

    def call(self, request: Callable[[object], object]):
        """`request(client)`, with SDK errors as the app's own. On Classark, a 401
        token_expired renews the access token once and tries again."""
        import anthropic

        for attempt in (1, 2):
            try:
                return request(self.client)
            except anthropic.AuthenticationError as e:
                if self.classark and attempt == 1 and classark_code(e) == "token_expired":
                    from qbx import account

                    try:
                        account.access_token(force=True)
                    except account.AccountError as renewal:
                        raise LLMError(str(renewal)) from renewal
                    continue
                raise map_error(e, self.classark) from e
            except anthropic.APIError as e:
                raise map_error(e, self.classark) from e
        raise AssertionError("unreachable")

    def params(self, prompt: str, schema: type[BaseModel]) -> dict:
        """The Messages API request body, shared by direct calls and batches."""
        params: dict = {
            "model": self.model,
            "max_tokens": MAX_TOKENS_THINK if self.think else MAX_TOKENS_QUICK,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {
                "effort": THINK_EFFORT.get(self.model, "high") if self.think else QUICK_EFFORT,
                "format": {"type": "json_schema", "schema": output_schema(schema)},
            },
        }
        if self.think:
            params["thinking"] = {"type": "adaptive"}
        return params

    def headers(self) -> dict:
        """Per-call headers: the shared cache's key on Classark tag calls."""
        label = llm_mod.current_cache_label()
        if self.classark and self.job == "tag" and label:
            return {"X-Classark-Cache": f"{self.job}:{label}"}
        return {}

    def _create(self, params: dict):
        extra = {"extra_headers": headers} if (headers := self.headers()) else {}
        if self.model in FALLBACK_MODELS:
            return self.call(lambda c: c.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default", **extra))
        return self.call(lambda c: c.messages.create(**params, **extra))

    def json(self, prompt: str, schema: type[T]) -> T:
        params = self.params(prompt, schema)
        last: Exception | None = None
        for _ in range(self.RETRIES):
            message = self._create(params)
            try:
                return parse_message(message, schema)
            except (ValidationError, ValueError) as e:
                log.warning("Claude's reply didn't fit %s, request_id=%s: %s",
                            schema.__name__, getattr(message, "_request_id", None), e)
                last = e
        raise LLMError(f"Claude's reply didn't fit after {self.RETRIES} attempts: {last}")
