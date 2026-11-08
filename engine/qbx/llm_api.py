"""Claude through the Anthropic API (the official `anthropic` SDK), with the user's API key.

The production provider. Same `LLM` protocol as `ClaudeLLM` (`json(prompt, schema)`), so
tagging, requests, chat, adjusting and fixing don't know which one they got. Every call is
structured output (`output_config.format` with the pydantic model's JSON schema), so a
reply is valid JSON for the schema or an error.

Thinking and effort, per model (the API's rules, not Claude Code's):
- `think=False` (tagging, a whole bank of papers): no `thinking` parameter, effort "low".
  Haiku 5.5, Sonnet 5.5 and Opus 5.5 all think adaptively by default and refuse
  `thinking: {"type": "disabled"}` in some combination (Sonnet 5.5 and Opus 5.5 always), so
  effort is the lever: at "low" the model thinks little or not at all, which keeps output
  near the ~1,300 tokens a paper's tags need.
- `think=True` (requests, fixing): `thinking: {"type": "adaptive"}` at the model's
  `THINK_EFFORT` ("high" for Sonnet and Opus, Haiku's default "medium").
Sonnet 5.5 and Opus 5.5 direct calls opt into the server-side refusal fallback
(`fallbacks: "default"`); Haiku has none, and the Batches API rejects the parameter.

Errors become the app's own (`map_error`): a 429 or overload, after the SDK's own retries,
is `UsageLimitError` with `resets_at` from `retry-after`, so background runs pause as they
do on a subscription's limit; a rejected key, a billing problem and a refusal are
`LLMError`s that say what to do. Each failure's `request_id` is logged.

Batches (`batch_params`, `parse_message`) are used by batch_tagging.py.
"""

import json
import logging
import time
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from qbx import api_key
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

# tests swap this for a fake: a callable returning a client with the SDK's shape
_client_factory: Callable[[], object] | None = None


def set_client_factory(factory: Callable[[], object] | None) -> None:
    global _client_factory
    _client_factory = factory


def make_client():
    """An Anthropic client for the saved (or environment) API key.

    The future Classark account provider is one more branch here: the same SDK client with
    `base_url=<the Classark server>` and `auth_token=api_key.get_secret("classark_token")`,
    since that server speaks the Messages API and proxies it to Anthropic.
    """
    if _client_factory is not None:
        return _client_factory()
    key = api_key.get_api_key()
    if not key:
        raise LLMError("No Anthropic API key is saved - add one in Settings.")
    import anthropic

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


def map_error(error: Exception) -> Exception:
    """An SDK exception as the app's own error, most specific first."""
    import anthropic

    request_id = getattr(error, "request_id", None)
    suffix = f" (request {request_id})" if request_id else ""
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

    def __init__(self, model: str, think: bool = True, client=None):
        self.model = model
        self.think = think
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

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

    def _create(self, params: dict):
        if self.model in FALLBACK_MODELS:
            return self.client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
        return self.client.messages.create(**params)

    def json(self, prompt: str, schema: type[T]) -> T:
        import anthropic

        params = self.params(prompt, schema)
        last: Exception | None = None
        for _ in range(self.RETRIES):
            try:
                message = self._create(params)
            except anthropic.APIError as e:
                raise map_error(e) from e
            try:
                return parse_message(message, schema)
            except (ValidationError, ValueError) as e:
                log.warning("Claude's reply didn't fit %s, request_id=%s: %s",
                            schema.__name__, getattr(message, "_request_id", None), e)
                last = e
        raise LLMError(f"Claude's reply didn't fit after {self.RETRIES} attempts: {last}")
