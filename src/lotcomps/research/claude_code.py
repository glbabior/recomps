"""Reading pages through Claude Code instead of the API.

Same job as `research.llm`, different billing. The API meters every token
against an API account; Claude Code, signed in to a Claude subscription, counts
usage against that plan instead. For someone who already pays for a
subscription and runs this tool for themselves a few times a month, that is the
difference between a per-run bill and no bill at all.

The catch worth knowing, because it is silent: **an `ANTHROPIC_API_KEY` in the
environment shadows the subscription.** Claude Code prefers the key, so a tool
that inherits the parent environment gets billed to the API account even though
the user is signed in to a plan. This module removes the key from the child
process's environment for exactly that reason.

Trade-offs against the API path, honestly:

* **Slower.** Each page is a separate process launch, not an HTTP request.
* **Heavier per call.** Claude Code carries its own system prompt, so a page
  costs more tokens here than it does through the API. On a subscription that
  is usage rather than money, but it is not free of consequence -- a large run
  will consume plan limits.
* **Less direct control** over model and sampling.

What does not change: the fetching still happens locally, so the politeness
rules are still enforced here rather than hoped for.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from lotcomps.research.llm import EXTRACTION_SYSTEM, ExtractionResult, Usage

T = TypeVar("T", bound=BaseModel)

#: Credentials that would shadow the subscription if inherited.
_SHADOWING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
DEFAULT_TIMEOUT_SECONDS = 300
#: Claude Code takes an alias here rather than a full model id.
DEFAULT_CLI_MODEL = "sonnet"


class ClaudeCodeUnavailable(RuntimeError):
    pass


def find_cli() -> str | None:
    return shutil.which("claude")


def subscription_status() -> dict[str, Any]:
    """What Claude Code would authenticate as, with the key removed.

    Returns the parsed status, or an empty dict if the CLI is missing or the
    call fails. Used to tell the user plainly which account will pay.
    """
    cli = find_cli()
    if cli is None:
        return {}
    try:
        completed = subprocess.run(
            [cli, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=60,
            env=_clean_env(),
            check=False,
        )
        return json.loads(completed.stdout or "{}")
    except Exception:
        return {}


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in _SHADOWING_VARS:
        env.pop(name, None)
    return env


def _parse_structured(raw: Any) -> dict[str, Any] | None:
    """Read the structured output field, which is not always strict JSON.

    Claude Code returns it as a string. Usually that string is JSON; sometimes
    it is a Python-repr dict (single quotes, ``None``, ``True``). Both are
    handled rather than one being treated as a failure.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, dict) else None


@dataclass
class ClaudeCodeExtractor:
    """Drives the Claude Code CLI to read one page into a schema."""

    model: str = DEFAULT_CLI_MODEL
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    #: Kept for interface parity with the API client; a subscription run does
    #: not spend money, but the notional cost is still tracked and reported.
    budget: Any = None
    notional_cost_usd: float = 0.0
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        from lotcomps.research.llm import Budget

        if self.budget is None:
            # No money ceiling on a subscription run, but the call ceiling
            # still applies -- an unbounded loop is a problem either way.
            self.budget = Budget(total=None, max_cost_usd=None)

    @property
    def available(self) -> bool:
        return find_cli() is not None and bool(subscription_status().get("loggedIn"))

    @property
    def pays_from(self) -> str:
        status = subscription_status()
        plan = status.get("subscriptionType")
        if plan:
            return f"{plan} subscription ({status.get('email', 'signed in')})"
        return "unknown account"

    def extract(
        self,
        page_text: str,
        schema: type[T],
        request: str,
        system: str = EXTRACTION_SYSTEM,
    ) -> ExtractionResult:
        cli = find_cli()
        if cli is None:
            return ExtractionResult(error="the `claude` CLI is not on PATH")

        json_schema = schema.model_json_schema()
        json_schema["additionalProperties"] = False
        prompt = f"{request}\n\n--- PAGE CONTENT ---\n{page_text}"

        command = [
            cli,
            "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(json_schema),
            # No tools: this is a reading task over text handed to it, and the
            # fetching stays on our side of the line where the politeness rules
            # can be enforced.
            "--tools", "",
            "--no-session-persistence",
            "--model", self.model,
            "--append-system-prompt", system,
        ]

        usage = Usage()
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=_clean_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            usage.calls, usage.failures = 1, 1
            self.usage.add(usage)
            return ExtractionResult(error=f"timed out after {self.timeout}s", usage=usage)
        except Exception as exc:
            usage.calls, usage.failures = 1, 1
            self.usage.add(usage)
            return ExtractionResult(error=f"{type(exc).__name__}: {exc}", usage=usage)

        usage.calls = 1
        if completed.returncode != 0:
            usage.failures = 1
            self.usage.add(usage)
            detail = (completed.stderr or completed.stdout or "").strip()[:300]
            return ExtractionResult(error=f"claude exited {completed.returncode}: {detail}",
                                    usage=usage)

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            usage.failures = 1
            self.usage.add(usage)
            return ExtractionResult(
                error=f"unreadable CLI output: {completed.stdout[:200]!r}", usage=usage
            )

        raw_usage = payload.get("usage") or {}
        usage.input_tokens = int(raw_usage.get("input_tokens") or 0)
        usage.output_tokens = int(raw_usage.get("output_tokens") or 0)
        usage.cache_read_tokens = int(raw_usage.get("cache_read_input_tokens") or 0)
        usage.cache_write_tokens = int(raw_usage.get("cache_creation_input_tokens") or 0)
        self.usage.add(usage)
        self.notional_cost_usd += float(payload.get("total_cost_usd") or 0.0)
        self.budget.spent.add(usage)

        if payload.get("is_error"):
            return ExtractionResult(
                error=str(payload.get("result") or "the CLI reported an error")[:300],
                usage=usage,
            )

        data = _parse_structured(payload.get("structured_output"))
        if data is None:
            return ExtractionResult(
                error="the CLI returned no structured output for this page", usage=usage
            )
        try:
            return ExtractionResult(parsed=schema.model_validate(data), usage=usage)
        except Exception as exc:
            return ExtractionResult(
                error=f"structured output did not fit the schema: {exc}", usage=usage
            )
