"""The one door to a language model, and the settings that open it.

Two providers (`Settings.provider`):
- "api": the Anthropic API (`AnthropicLLM` in llm_api.py). Tagging goes through the Message
  Batches API (batch_tagging.py), at half price. Not offered in Settings: the app takes no
  API keys (user-decided). It's the base for the Classark account provider, which sends the
  same requests to the Classark server (identity federation to Anthropic, no stored key).
- "claude_code": Claude through the Claude Agent SDK and the Claude Code install on this
  machine (`ClaudeLLM`). With no API key set, calls run on that machine's own Claude
  subscription login and count against its usage limits - kept for the user and friends
  testing on their own machines. The default, until the Classark account provider exists.
A third, later: the user's own server proxying the API for subscribing teachers - a base
URL and an account token, one more branch in `llm_api.make_client`.

Opt-in: nothing is sent until "Use Claude" is switched on in Settings. Claude has three
jobs only (see CLAUDE.md, "AI features"), and never extracts or writes questions:

- tagging question parts with syllabus topics, and which earlier parts each one needs;
- reading a syllabus's topic headings;
- reading a typed exam request into a spec (exam_request.py);
- reading an instruction to change an exam into a plan (exam_adjust.py) - code picks the questions;
- reviewing a paper's open issues and proposing fixes, which the checks then test
  (ai_fix.py, fixes.py) - it moves, joins and numbers what the parser found, never writes it.

Every call asks for JSON matching a pydantic model (the SDK's structured output), so a
reply is either valid or an error, never free text to be parsed. Tests use `FakeLLM` and
never start Claude.
"""

import asyncio
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError, field_validator

# settings.json and topics/ live here - outside out/, which re-extraction regenerates.
CONFIG_DIR = Path(os.environ.get("QBX_CONFIG_DIR", Path(__file__).parent.parent.parent / "config")).resolve()
# Pinned model ids rather than Claude Code's aliases ("haiku"), which follow whatever Claude
# Code's install maps them to. Tagging is picking from a fixed list: Haiku does it well and
# stretches the subscription's usage furthest. Reading a request is one call per exam.
TAG_MODEL = "claude-haiku-5-5"
REQUEST_MODEL = "claude-sonnet-5-5"
MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
# what a settings.json saved before the ids were pinned holds
_ALIASES = {"haiku": TAG_MODEL, "sonnet": REQUEST_MODEL, "opus": "claude-opus-5-5"}

MAX_FIX_WORKERS = 6

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """A model call failed, or replied with something unusable."""


class UsageLimitError(LLMError):
    """The subscription's usage limit, or the API's rate limit, is reached - wait for it to
    reset (`resets_at`), don't retry."""

    def __init__(self, message: str, resets_at: float | None = None):
        super().__init__(message)
        self.resets_at = resets_at


PROVIDERS = ["api", "claude_code"]


class Settings(BaseModel):
    enabled: bool = False
    # a fresh install uses the API; load_settings reads a file without it as claude_code
    # Claude Code until the Classark account provider exists: the app takes no API keys
    # (user-decided), so "api" is only for the server-side proxy work and tests
    provider: Literal["api", "claude_code"] = "claude_code"
    tag_model: str = TAG_MODEL
    request_model: str = REQUEST_MODEL
    # reviewing papers' issues (qbx/ai_fix.py): "auto" applies the fixes the checks confirm,
    # "review" leaves every one for the teacher to accept
    # Sonnet, not Haiku: fixing is reasoning (where a missed "(ii)" starts, which row is
    # whose, do the marks add up), and once the parser cleared most papers it runs on a
    # handful - Haiku with thinking off missed most of what it was asked
    fix_model: str = REQUEST_MODEL
    fix_mode: Literal["auto", "review"] = "auto"
    # papers reviewed at once, each its own Claude Code process. More is faster until the
    # plan's rate limit starts refusing them.
    fix_workers: int = 2

    @field_validator("fix_workers")
    @classmethod
    def _clamp_workers(cls, value: int) -> int:
        return max(1, min(MAX_FIX_WORKERS, value))

    @field_validator("tag_model", "request_model", "fix_model")
    @classmethod
    def _pin_alias(cls, value: str) -> str:
        return _ALIASES.get(value, value)


_settings_lock = threading.Lock()


def _settings_path() -> Path:
    return CONFIG_DIR / "settings.json"


def load_settings() -> Settings:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if isinstance(data, dict) and "provider" not in data:
        data["provider"] = "claude_code"  # saved before the API provider existed
    try:
        return Settings.model_validate(data)
    except ValueError:
        return Settings()


def save_settings(settings: Settings) -> None:
    with _settings_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _settings_path().write_text(settings.model_dump_json(indent=2), encoding="utf-8")


class LLM(Protocol):
    def json(self, prompt: str, schema: type[T]) -> T: ...


def _limit_message(resets_at: float | None) -> str:
    when = ""
    if resets_at:
        when = " It resets at " + datetime.datetime.fromtimestamp(resets_at).strftime("%H:%M") + "."
    return "Your Claude usage limit is reached." + when


_windows_hidden = False


def _hide_cli_windows() -> None:
    """Starts the Claude Code CLI with no console window on Windows.

    The Agent SDK starts the CLI once per request through anyio.open_process, with no way
    to pass creation flags. A console program started from the packaged app - a windowed
    exe, with no console of its own - gets a brand-new console window, so every tagging
    call flashed a terminal on screen (reported by the user). CREATE_NO_WINDOW gives it a
    hidden console instead, which anything the CLI starts in turn shares.

    anyio.open_process is wrapped rather than the SDK's private transport module, and only
    fills in flags a caller didn't set - nothing else in the app starts processes this way.
    """
    global _windows_hidden
    if sys.platform != "win32" or _windows_hidden:
        return
    import anyio

    original = anyio.open_process

    async def open_process_hidden(*args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return await original(*args, **kwargs)

    anyio.open_process = open_process_hidden
    _windows_hidden = True


class ClaudeLLM:
    SYSTEM = (
        "You help a teacher organise past exam papers. Answer only from what you are given. "
        "Give your answer only through the structured output tool - do not write it out as "
        "text first, and write nothing else."
    )
    RETRIES = 3

    def __init__(self, model: str, think: bool = True):
        self.model = model
        # Measured on the first ~200 tagging requests (Claude Code's own transcripts): Haiku
        # thought ~10,500 tokens per paper before a ~1,300-token answer, and typed that
        # answer out as text before giving it again as structured output - ~13,100 output
        # tokens a paper instead of ~1,300, which ran the user's subscription limit out
        # twice. Tagging picks from a given topic list, so it runs with thinking off.
        self.think = think

    async def _ask(self, prompt: str, schema: type[T]) -> T:
        from claude_agent_sdk import ClaudeAgentOptions, RateLimitEvent, ResultMessage, query

        _hide_cli_windows()
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=self.SYSTEM,
            tools=[],  # it only reads what's in the prompt
            setting_sources=[],  # no CLAUDE.md, skills or hooks from this machine
            cwd=tempfile.gettempdir(),
            max_turns=3,
            thinking=None if self.think else {"type": "disabled"},
            output_format={"type": "json_schema", "schema": schema.model_json_schema()},
        )
        result: ResultMessage | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, RateLimitEvent) and message.rate_limit_info.status == "rejected":
                resets = message.rate_limit_info.resets_at
                raise UsageLimitError(_limit_message(resets), resets)
            if isinstance(message, ResultMessage):
                result = message
        if result is None:
            raise LLMError("Claude gave no answer.")
        if result.is_error:
            detail = "; ".join(str(e) for e in (result.errors or [])) or str(result.result or result.subtype)
            if result.api_error_status == 429 or "limit" in detail.lower():
                raise UsageLimitError(_limit_message(None) + f" ({detail})")
            raise LLMError(f"Claude couldn't answer: {detail}")
        data = result.structured_output
        if data is None:  # fall back to the reply text, if it is the JSON
            data = json.loads(result.result or "null")
        return schema.model_validate(data)

    def json(self, prompt: str, schema: type[T]) -> T:
        last: Exception | None = None
        for _ in range(self.RETRIES):
            try:
                return asyncio.run(self._ask(prompt, schema))
            except LLMError:
                raise
            except (ValidationError, ValueError) as e:
                last = e  # a malformed reply is worth another try
            except Exception as e:
                if type(e).__name__ == "CLINotFoundError":
                    raise LLMError("Claude Code isn't installed on this computer - install it, then log in by running `claude`.") from e
                last = e
        raise LLMError(f"Claude failed after {self.RETRIES} attempts: {last}")


class FakeLLM:
    """For tests: `respond(prompt, schema)` returns a dict (or model) per call; every call
    is recorded in `calls`."""

    def __init__(self, respond: Callable[[str, type[BaseModel]], dict | BaseModel]):
        self.respond = respond
        self.calls: list[tuple[str, type[BaseModel]]] = []

    def json(self, prompt: str, schema: type[T]) -> T:
        self.calls.append((prompt, schema))
        reply = self.respond(prompt, schema)
        return reply if isinstance(reply, schema) else schema.model_validate(json.loads(json.dumps(reply)))


# tests swap this for a FakeLLM
_override: LLM | None = None


def set_llm_override(llm: LLM | None) -> None:
    global _override
    _override = llm


def make_llm(settings: Settings, job: str = "tag") -> LLM:
    """The model for a job ("tag", "request" or "fix") on the settings' provider, whether or
    not Claude is switched on (the Settings screen's "Test connection" asks before that)."""
    if settings.provider == "api":
        from qbx.llm_api import AnthropicLLM as make
    else:
        make = ClaudeLLM
    if job == "request":
        # one short call per typed request, where reasoning about topics and marks pays off
        return make(settings.request_model)
    if job == "fix":
        # one call per paper the parser left with issues - a handful, not a bank - so with
        # thinking on: placing a split and checking the marks is worth it
        return make(settings.fix_model, think=True)
    return make(settings.tag_model, think=False)


def get_llm(job: str = "tag") -> LLM | None:
    """The model for a job ("tag", "request" or "fix"), or None while AI is switched off."""
    if _override is not None:
        return _override
    from qbx.edition import claude_available

    if not claude_available():
        return None  # a release build: no Claude Code, and Classark accounts aren't built yet
    settings = load_settings()
    if not settings.enabled:
        return None
    return make_llm(settings, job)
