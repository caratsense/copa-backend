"""
AI Order Parser — STUB

Currently returns empty/low-confidence results.
When you're ready to plug in an LLM:

1. Set AI_PROVIDER=openai (or "custom") in .env
2. Implement the provider function below
3. The API endpoint stays the same

Example LLM prompt you'd send:
    "Extract structured order info from this text.
     Return JSON with: size, flavor, design, addons, rush, date.
     Text: '{user_text}'"
"""

from app.schemas import AIParseRequest, AIParseResponse
from app.config import get_settings

settings = get_settings()


def parse_order_text(request: AIParseRequest) -> AIParseResponse:
    """Route to the configured AI provider."""

    if settings.AI_PROVIDER == "openai":
        return _parse_with_openai(request.text)
    elif settings.AI_PROVIDER == "custom":
        return _parse_with_custom(request.text)
    else:
        return _parse_stub(request.text)


def _parse_stub(text: str) -> AIParseResponse:
    """
    Stub parser — returns the raw text with zero confidence.
    Replace this with your LLM call.
    """
    return AIParseResponse(
        raw_text=text,
        confidence=0.0,
        provider="stub",
    )


def _parse_with_openai(text: str) -> AIParseResponse:
    """
    TODO: Implement OpenAI / Anthropic call here.
    
    Example:
        import openai
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": f"Extract order: {text}"}]
        )
        parsed = json.loads(response.choices[0].message.content)
        return AIParseResponse(**parsed, raw_text=text, confidence=0.9, provider="openai")
    """
    raise NotImplementedError("OpenAI provider not configured yet. Set AI_PROVIDER=stub to use the stub.")


def _parse_with_custom(text: str) -> AIParseResponse:
    """
    TODO: Implement your custom LLM / NLP pipeline here.
    """
    raise NotImplementedError("Custom provider not implemented yet.")
