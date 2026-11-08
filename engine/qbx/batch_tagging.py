"""Tagging through the Message Batches API, when the provider is the Anthropic API.

Tagging a bank is non-interactive - nobody is waiting on a given paper - so instead of a
call per paper it sends every pending paper's tagging prompts as one Message Batch, at
half the price of direct calls (the user's decision). Exam requests, chat, adjustments and
fixing stay direct calls: a teacher is waiting on those, and fixing is multi-round and
verified between rounds (moving it to batches is a follow-up).

The flow, all inside the tagging run's background thread (main._run_tagging):
1. A batch already submitted - recorded in config/batches.json - is collected first, so an
   app restart carries on polling the batch it left behind rather than paying twice.
2. The papers still needing tags go into one new batch, one request per prompt
   (tagging.tagging_prompts, usually one per paper). Its id and what each request covers,
   keyed by `custom_id`, are saved before anything else happens.
3. Polled every POLL_SECONDS. Progress shows in the tagging state as "Waiting for Claude's
   batch: n of m done".
4. Once it ends, results are read in whatever order they come, matched by `custom_id`,
   checked like a direct reply (tagging.apply_reply) and saved paper by paper.
5. A request that errored, expired or replied with something unusable goes into the next
   batch; after MAX_BATCH_ATTEMPTS batches - or straight away for an invalid request,
   which a batch would only refuse again - it's a direct call.
Stopping while a batch runs leaves it running at Anthropic, still recorded: starting
tagging again (or the app's next start) collects it.
"""

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

from pydantic import ValidationError

from qbx import llm as llm_mod
from qbx.llm import LLMError
from qbx.schema import Paper
from qbx.tagging import PaperTags, apply_reply, tag_questions, tagging_prompts
from qbx.topics import Topic

BATCHES_FILE = "batches.json"
POLL_SECONDS = 30.0
MAX_BATCH_ATTEMPTS = 2

_file_lock = threading.Lock()


@dataclass
class Hooks:
    """What the run needs from the app (main.py), so this module holds no app state."""

    load_paper: Callable[[str], Paper]
    topics_for: Callable[[str], list[Topic]]  # by paper key
    save: Callable[[str, dict[str, dict]], None]  # fresh tags for a paper
    update: Callable[..., None]  # the tagging state
    add_done: Callable[[int], None]  # questions tagged
    fail: Callable[[str, str], None]  # paper key, error
    stopping: Callable[[], bool]


# -- the record of a submitted batch -----------------------------------------------------


def _path():
    return llm_mod.CONFIG_DIR / BATCHES_FILE


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    with _file_lock:
        llm_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(_path())


def pending() -> dict | None:
    """The tagging batch submitted and not yet collected, if any."""
    record = _read().get("tagging")
    return record if isinstance(record, dict) and record.get("batch_id") else None


def _save_pending(record: dict | None) -> None:
    data = _read()
    if record is None:
        data.pop("tagging", None)
    else:
        data["tagging"] = record
    _write(data)


# -- the run -----------------------------------------------------------------------------

Work = tuple[str, list[int], int]  # paper key, question numbers, attempt


def _api(call, *args, **kwargs):
    import anthropic

    from qbx.llm_api import map_error

    try:
        return call(*args, **kwargs)
    except anthropic.APIError as e:
        raise map_error(e) from e


def _submit(llm, work: list[Work], hooks: Hooks) -> dict | None:
    requests, covers = [], {}
    for paper_key, numbers, attempt in work:
        paper = hooks.load_paper(paper_key)
        questions = [q for q in paper.questions if q.question_number in numbers]
        for prompt, chunk in tagging_prompts(paper, questions, hooks.topics_for(paper_key)):
            custom_id = f"r{len(requests)}"  # paper keys aren't always valid custom ids
            requests.append({"custom_id": custom_id, "params": llm.params(prompt, PaperTags)})
            covers[custom_id] = {
                "paper_key": paper_key, "numbers": [q.question_number for q in chunk], "attempt": attempt,
            }
    if not requests:
        return None
    batch = _api(llm.client.messages.batches.create, requests=requests)
    record = {"batch_id": batch.id, "model": llm.model, "submitted_at": time.time(), "requests": covers}
    _save_pending(record)
    return record


def _wait(llm, record: dict, hooks: Hooks) -> bool:
    """Polls until the batch ends (True) or the run is stopped (False)."""
    total = len(record["requests"])
    while True:
        batch = _api(llm.client.messages.batches.retrieve, record["batch_id"])
        counts = batch.request_counts
        done = total - getattr(counts, "processing", 0)
        hooks.update(
            stage=f"Waiting for Claude's batch: {done} of {total} done",
            batch={"id": record["batch_id"], "done": done, "total": total},
        )
        if batch.processing_status == "ended":
            return True
        deadline = time.monotonic() + POLL_SECONDS
        while True:
            if hooks.stopping():
                return False
            if time.monotonic() >= deadline:
                break
            time.sleep(min(1.0, POLL_SECONDS))


def _collect(llm, record: dict, hooks: Hooks) -> list[Work]:
    """Applies a finished batch's results; returns what needs asking again."""
    from qbx.llm_api import parse_message

    covers: dict[str, dict] = record["requests"]
    seen: set[str] = set()
    again: list[Work] = []
    for item in _api(llm.client.messages.batches.results, record["batch_id"]):
        cover = covers.get(item.custom_id)
        if cover is None or item.custom_id in seen:
            continue
        seen.add(item.custom_id)
        paper_key, numbers, attempt = cover["paper_key"], cover["numbers"], cover["attempt"]
        result = item.result
        if result.type == "succeeded":
            try:
                reply = parse_message(result.message, PaperTags)
            except LLMError as e:  # a refusal or a cut-off reply: once more, directly
                again.append((paper_key, numbers, max(attempt + 1, MAX_BATCH_ATTEMPTS + 1)))
                hooks.update(last_batch_error=str(e)[:200])
                continue
            except (ValidationError, ValueError):
                again.append((paper_key, numbers, attempt + 1))
                continue
            paper = hooks.load_paper(paper_key)
            questions = [q for q in paper.questions if q.question_number in numbers]
            fresh = apply_reply(questions, reply, hooks.topics_for(paper_key))
            if fresh:
                hooks.save(paper_key, fresh)
            hooks.add_done(len(questions))
        elif result.type == "errored":
            error = getattr(getattr(result, "error", None), "error", None)
            invalid = getattr(error, "type", None) == "invalid_request_error"
            again.append((paper_key, numbers, MAX_BATCH_ATTEMPTS + 1 if invalid else attempt + 1))
        else:  # expired or canceled
            again.append((paper_key, numbers, attempt + 1))
    # a request the results never mentioned is asked again too
    for custom_id, cover in covers.items():
        if custom_id not in seen:
            again.append((cover["paper_key"], cover["numbers"], cover["attempt"] + 1))
    _save_pending(None)
    return again


def _direct(llm, work: list[Work], hooks: Hooks) -> None:
    for paper_key, numbers, _ in work:
        if hooks.stopping():
            return
        paper = hooks.load_paper(paper_key)
        questions = [q for q in paper.questions if q.question_number in numbers]
        hooks.update(stage="Tagging the rest directly", current=paper_key)
        try:
            fresh = tag_questions(llm, paper, questions, hooks.topics_for(paper_key))
        except llm_mod.UsageLimitError:
            raise
        except Exception as e:
            hooks.fail(paper_key, str(e)[:300])
            continue
        if fresh:
            hooks.save(paper_key, fresh)
        hooks.add_done(len(questions))


def run(llm, find_todo: Callable[[], list[tuple[str, list[int]]]], hooks: Hooks) -> None:
    """Tags everything `find_todo` lists (paper key, question numbers) through batches.
    Raises UsageLimitError for the caller to pause on, as a direct call would."""
    attempts: dict[tuple[str, tuple[int, ...]], int] = {}
    record = pending()
    if record is not None:
        hooks.update(stage="Collecting Claude's batch")
        if not _wait(llm, record, hooks):
            return
        for paper_key, numbers, attempt in _collect(llm, record, hooks):
            attempts[(paper_key, tuple(numbers))] = attempt

    todo = find_todo()
    work: list[Work] = []
    for paper_key, numbers in todo:
        # a retry keeps its attempt count; it may cover part of the paper's questions
        retried = [(n, a) for (k, n), a in attempts.items() if k == paper_key]
        if retried:
            left = set(numbers)
            for chunk, attempt in retried:
                in_chunk = [n for n in chunk if n in left]
                if in_chunk:
                    work.append((paper_key, in_chunk, attempt))
                    left -= set(in_chunk)
            if left:
                work.append((paper_key, sorted(left), 1))
        else:
            work.append((paper_key, numbers, 1))

    while work and not hooks.stopping():
        _direct(llm, [w for w in work if w[2] > MAX_BATCH_ATTEMPTS], hooks)
        batched = [w for w in work if w[2] <= MAX_BATCH_ATTEMPTS]
        if not batched or hooks.stopping():
            return
        hooks.update(stage="Sending the papers to Claude as a batch", current=None)
        record = _submit(llm, batched, hooks)
        if record is None:
            return
        if not _wait(llm, record, hooks):
            return
        work = _collect(llm, record, hooks)
