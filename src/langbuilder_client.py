"""LangBuilder client for communicating with the LLM flow.

Uses the synchronous Run API with tweaks:
POST /api/v1/run/{flow_id} → returns full response directly
"""

import json
import logging
from typing import Any, Optional

import httpx

from .db.models import LLMResponse

logger = logging.getLogger(__name__)


class LangBuilderError(Exception):
    """Base exception for LangBuilder errors."""
    pass


class LangBuilderTimeoutError(LangBuilderError):
    """Timeout communicating with LangBuilder."""
    pass


class LangBuilderAPIError(LangBuilderError):
    """API error from LangBuilder."""
    def __init__(self, message: str, status_code: int, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class LangBuilderClient:
    """Client for communicating with LangBuilder flows.

    Uses the Run API which returns results directly:
    POST /api/v1/run/{flow_id} → full response
    """

    # ChatInput component ID in the LangBuilder flow
    CHAT_INPUT_ID = "ChatInput-UMrKl"

    def __init__(
        self,
        flow_url: str,
        flow_id: str,
        api_key: Optional[str] = None,
        timeout: int = 300,
        poll_interval: int = 5,
        max_poll_attempts: int = 60,
    ):
        self.flow_url = flow_url.rstrip("/")
        self.flow_id = flow_id
        self.api_key = api_key
        self.timeout = timeout

    @property
    def run_endpoint(self) -> str:
        """Endpoint URL for running the flow."""
        return f"{self.flow_url}/api/v1/run/{self.flow_id}"

    def _get_headers(self) -> dict[str, str]:
        """Get headers for API requests."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    async def run_flow(
        self,
        session_id: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Run the LangBuilder flow with the given input using Run API.

        Args:
            session_id: UUID for the session
            input_data: Dictionary containing:
                - command: The command ("/jira-sync" or "approval_decisions")
                - messages: List of message texts (for /jira-sync)
                - decisions: List of approval decisions (for approval_decisions)

        Returns:
            Raw response from LangBuilder
        """
        input_value_str = json.dumps(input_data)

        # Build payload for Run API with tweaks
        payload = {
            "output_type": "chat",
            "input_type": "chat",
            "session_id": session_id,
            "tweaks": {
                self.CHAT_INPUT_ID: {
                    "input_value": input_value_str
                }
            }
        }

        logger.info("=" * 60)
        logger.info("LANGBUILDER RUN REQUEST")
        logger.info("=" * 60)
        logger.info("Session ID: %s", session_id)
        logger.info("Run endpoint: %s", self.run_endpoint)
        logger.info("Input value: %s", input_value_str)
        logger.info("=" * 60)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.run_endpoint,
                    json=payload,
                    headers=self._get_headers(),
                )

                logger.info("Response status: %d", response.status_code)

                if response.status_code == 200:
                    data = response.json()
                    logger.info("Run completed successfully")

                    # Log the AI response preview
                    try:
                        ai_text = data.get("outputs", [{}])[0].get("outputs", [{}])[0].get("artifacts", {}).get("message", "")
                        if ai_text:
                            logger.info("AI response (first 500 chars):\n%s", ai_text[:500])
                    except (IndexError, KeyError, TypeError):
                        pass

                    return data
                else:
                    error_text = response.text[:500]
                    logger.error("Run failed: %d - %s", response.status_code, error_text)
                    raise LangBuilderAPIError(
                        f"Run failed: {response.status_code}",
                        response.status_code,
                        error_text
                    )

        except httpx.TimeoutException as e:
            logger.error("Request timeout after %d seconds", self.timeout)
            raise LangBuilderTimeoutError(f"Request timeout: {e}")
        except httpx.RequestError as e:
            logger.error("Request error: %s", str(e))
            raise LangBuilderError(f"Request error: {e}")

    async def send_continuation(
        self,
        session_id: str,
        message: str,
    ) -> dict[str, Any]:
        """
        Send a continuation message (not a /jira-sync command).

        Used for follow-up interactions like approve/reject button responses.

        Args:
            session_id: UUID for the session
            message: The continuation message (JSON string)

        Returns:
            Raw response from LangBuilder
        """
        try:
            input_data = json.loads(message)
        except json.JSONDecodeError:
            input_data = {"message": message}

        logger.info("Sending continuation to session %s", session_id)
        return await self.run_flow(session_id, input_data)


def parse_llm_response(raw_response: dict[str, Any]) -> LLMResponse:
    """
    Parse the raw LangBuilder response into a structured LLMResponse.

    The LLM is expected to return a JSON string with this structure:
    {
        "analysis_summary": "Found X tickets...",
        "proposals": [...],
        "no_action_items": [...]
    }
    """
    message_content = None

    try:
        outputs = raw_response.get("outputs", [])
        if outputs:
            inner_outputs = outputs[0].get("outputs", [])
            if inner_outputs:
                result = inner_outputs[0]

                # Path 1: artifacts.message (most common)
                if "artifacts" in result and isinstance(result["artifacts"], dict):
                    msg = result["artifacts"].get("message")
                    if isinstance(msg, str) and msg.strip():
                        message_content = msg.strip()

                # Path 2: messages[0].message
                if not message_content and "messages" in result:
                    messages = result["messages"]
                    if isinstance(messages, list) and messages:
                        first_msg = messages[0]
                        if isinstance(first_msg, dict):
                            msg = first_msg.get("message")
                            if isinstance(msg, str) and msg.strip():
                                message_content = msg.strip()

                # Path 3: results.message.text
                if not message_content and "results" in result:
                    results = result["results"]
                    if isinstance(results, dict):
                        message_obj = results.get("message")
                        if isinstance(message_obj, dict):
                            text = message_obj.get("text")
                            if isinstance(text, str) and text.strip():
                                message_content = text.strip()
                        elif isinstance(message_obj, str) and message_obj.strip():
                            message_content = message_obj.strip()

    except Exception as e:
        logger.error("Error navigating response structure: %s", e)

    # Fallback: Direct message field
    if not message_content:
        message_content = raw_response.get("message") or raw_response.get("text")

    if not message_content:
        logger.error("Could not extract message from response: %s",
                     str(raw_response)[:500])
        return LLMResponse(
            session_id="",
            analysis_summary="Error: Could not parse LLM response",
            proposals=[],
            error="Failed to extract message from response",
        )

    # Parse the JSON from the message content
    try:
        content = message_content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        data = json.loads(content)

        return LLMResponse(
            session_id=data.get("session_id", ""),
            analysis_summary=data.get("analysis_summary", ""),
            proposals=data.get("proposals", []),
            no_action_items=data.get("no_action_items", []),
        )

    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response as JSON: %s", str(e))
        logger.debug("Raw content: %s", message_content[:500])

        return LLMResponse(
            session_id="",
            analysis_summary=message_content[:200],
            proposals=[],
            error=f"JSON parse error: {str(e)}",
        )
