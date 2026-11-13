from contextlib import contextmanager
from contextvars import ContextVar

from core import config

_fixtures: ContextVar[dict[str, str] | None] = ContextVar("review_fixtures", default=None)


class LLMError(RuntimeError):
    pass


@contextmanager
def fixture_responses(responses: dict[str, str]):
    token = _fixtures.set(dict(responses))
    try:
        yield
    finally:
        _fixtures.reset(token)


def response_source() -> str:
    return "review_fixture" if _fixtures.get() is not None else "github_models"


def call_llm(prompt: str, *, stage: str | None = None) -> str:
    responses = _fixtures.get()
    if responses is not None:
        if stage not in responses or not responses[stage].strip():
            raise LLMError("Missing offline review fixture; live fallback is disabled.")
        return responses[stage]
    if config.LLM_PROVIDER != "github":
        raise LLMError("This CLI supports GitHub Models. Set LLM_PROVIDER=github in your local .env.")
    if not config.GITHUB_TOKEN or config.GITHUB_TOKEN.startswith("your_"):
        raise LLMError("Set GITHUB_TOKEN privately in your local .env before a live run.")
    try:
        from langchain_openai import ChatOpenAI

        client = ChatOpenAI(
            api_key=config.GITHUB_TOKEN,
            base_url=config.GITHUB_BASE_URL,
            model=config.GITHUB_MODEL,
            temperature=0,
            max_retries=0,
            timeout=60,
        )
        content = client.invoke(prompt).content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Empty or non-text response")
        return content
    except Exception:
        raise LLMError("GitHub Models request failed. Check model access, token validity and rate limits.") from None