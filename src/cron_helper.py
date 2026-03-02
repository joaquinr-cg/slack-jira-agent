"""Natural language to cron expression converter using Claude API."""

import logging

import httpx

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Convert the schedule description to a 5-field cron expression. "
    "Return ONLY the cron expression, nothing else. "
    "If the input is already a valid cron expression, return it as-is."
)


async def natural_language_to_cron(text: str, api_key: str) -> str:
    """Convert a natural language schedule description to a cron expression.

    Calls Claude Haiku to interpret the input. On any error, returns the
    original text so that downstream croniter validation can catch it.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 32,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": text}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            cron = data["content"][0]["text"].strip()
            logger.info("Converted '%s' -> '%s'", text, cron)
            return cron
    except Exception:
        logger.exception("LLM cron conversion failed, falling back to raw input")
        return text
