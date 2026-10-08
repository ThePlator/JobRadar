"""LiteLLM wrapper: prompt files, JSON output validated against a schema, cache, daily budget.

- Prompts live in `prompts/*.md`; the first line is `version: <id>`. Changing the version
  changes the cache key, so results are recomputed.
- Output is JSON validated with Pydantic. One repair retry shows the model its errors;
  a second failure raises SchemaError (the job is marked needs_review).
- Spend is estimated at list prices and capped per day (config `llm.daily_budget_inr`);
  when the cap is hit, LLM tasks wait until midnight in the configured timezone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from importlib import resources
from typing import Any, TypeVar

import litellm
from litellm import exceptions as llm_errors
from pydantic import BaseModel, ValidationError

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.config import ConfigError, Settings
from jobradar.db.repo import Repo
from jobradar.pipeline.dedupe import sha1
from jobradar.queue.tasks import Defer, PermanentError, RetryableError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

litellm.suppress_debug_info = True
litellm.telemetry = False

_PROVIDER_KEY = {"gemini": "gemini_api_key", "groq": "groq_api_key"}
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class SchemaError(Exception):
    """The model did not return valid JSON for the schema, even after one repair."""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    template: str

    def render(self, **values: str) -> str:
        text = self.template
        for key, value in values.items():
            # Untrusted text must not be able to close the tag it sits in.
            safe = re.sub(r"</?\s*(posting|message|posted_on|job|candidate|profile)\b", "", value)
            text = text.replace("{{" + key + "}}", safe)
        return text


@cache
def load_prompt(name: str) -> Prompt:
    raw = resources.files("jobradar.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    if not first.startswith("version:"):
        raise ValueError(f"prompts/{name}.md must start with 'version: <id>'")
    return Prompt(name, first.removeprefix("version:").strip(), rest.lstrip("\n"))


def _parse_json(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(content)  # tolerate ```json fences or a stray sentence
        if match:
            return json.loads(match.group(0))
        raise


class LLM:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        clock: Clock = utcnow,
        completion: Any = None,
    ) -> None:
        self._cfg = settings.config.llm
        self._secrets = settings.secrets
        self._tz = settings.config.notify.tz
        self._repo = repo
        self._clock = clock
        self._completion = completion or litellm.acompletion
        self._interval = 60.0 / self._cfg.requests_per_minute
        self._next_call = 0.0
        self._lock = asyncio.Lock()

    def _api_key(self, model: str) -> str | None:
        provider = model.split("/", 1)[0]
        field = _PROVIDER_KEY.get(provider)
        secret = getattr(self._secrets, field) if field else None
        if field and secret is None:
            raise ConfigError(f"Missing in .env: {field.upper()} (needed for {model})")
        return secret.get_secret_value() if secret else None

    def _midnight_utc(self) -> datetime:
        local = self._clock().astimezone(self._tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def _check_budget(self) -> None:
        start = self._midnight_utc()
        spent = self._repo.llm_spend_since(to_iso(start))
        if spent >= self._cfg.daily_budget_inr:
            wait = (start + timedelta(days=1) - self._clock()).total_seconds()
            log.warning(
                "daily LLM budget reached; pausing until midnight",
                extra={"spent_inr": round(spent, 2), "budget_inr": self._cfg.daily_budget_inr},
            )
            raise Defer(max(wait, 60))

    async def _pace(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next_call > now:
                await asyncio.sleep(self._next_call - now)
            self._next_call = max(now, self._next_call) + self._interval

    def _cost_inr(self, model: str, response: Any) -> float:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0.0
        try:
            cost_in, cost_out = litellm.cost_per_token(
                model=model,
                prompt_tokens=int(usage.prompt_tokens or 0),
                completion_tokens=int(usage.completion_tokens or 0),
            )
        except Exception:  # unknown model price: count it as free rather than fail
            return 0.0
        return float(cost_in + cost_out) * self._cfg.usd_to_inr

    async def _call(self, model: str, messages: list[dict[str, str]]) -> tuple[str, float]:
        self._check_budget()
        await self._pace()
        try:
            response = await self._completion(
                model=model,
                messages=messages,
                api_key=self._api_key(model),
                response_format={"type": "json_object"},
                timeout=60,
            )
        except llm_errors.RateLimitError as e:
            raise RetryableError(f"LLM rate limit: {e}", retry_after=60) from e
        except (
            llm_errors.Timeout,
            llm_errors.APIConnectionError,
            llm_errors.ServiceUnavailableError,
            llm_errors.InternalServerError,
        ) as e:
            raise RetryableError(f"LLM unavailable: {e}") from e
        except (llm_errors.AuthenticationError, llm_errors.PermissionDeniedError) as e:
            raise PermanentError(f"LLM key rejected for {model}: check .env") from e
        except llm_errors.BadRequestError as e:
            raise PermanentError(f"LLM rejected the request: {e}") from e
        content = response.choices[0].message.content or ""
        return content, self._cost_inr(model, response)

    async def structured(self, model: str, prompt: Prompt, schema: type[T], **values: str) -> T:
        """Run a prompt and return the validated object (cached by model, version, input)."""
        text = prompt.render(**values)
        key = sha1(f"{model}|{prompt.version}|{text}")
        if (cached := self._repo.llm_cache_get(key)) is not None:
            try:
                return schema.model_validate_json(cached)
            except ValidationError:
                pass  # schema changed since: recompute

        messages = [{"role": "user", "content": text}]
        content, cost = await self._call(model, messages)
        try:
            result = schema.model_validate(_parse_json(content))
        except (json.JSONDecodeError, ValidationError) as first_error:
            log.info("LLM output invalid; asking for a repair", extra={"prompt": prompt.name})
            messages += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "That was not valid for the requested JSON format: "
                    f"{str(first_error)[:800]}\nReturn only the corrected JSON object.",
                },
            ]
            content, more = await self._call(model, messages)
            cost += more
            try:
                result = schema.model_validate(_parse_json(content))
            except (json.JSONDecodeError, ValidationError) as e:
                self._repo.llm_cache_put(f"failed:{key}", content, cost)  # still count the spend
                raise SchemaError(str(e)[:500]) from e

        self._repo.llm_cache_put(key, result.model_dump_json(), cost)
        return result
