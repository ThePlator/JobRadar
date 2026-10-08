"""Small stand-ins for the LLM provider and Notion settings used across tests."""

import json
from types import SimpleNamespace
from typing import Any

from jobradar.config import AppConfig, Secrets, Settings


def llm_settings(**llm: Any) -> Settings:
    return Settings(
        config=AppConfig.model_validate(
            {
                "sources": {"telegram": {"enabled": False}},
                "llm": {"requests_per_minute": 1000, **llm},
            }
        ),
        secrets=Secrets.model_validate({"gemini_api_key": "test-key"}),
    )


class FakeCompletion:
    """Returns queued replies (dicts become JSON); records every call."""

    def __init__(self, *replies: Any, prompt_tokens: int = 1000, completion_tokens: int = 200):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.usage = SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=self.usage
        )
