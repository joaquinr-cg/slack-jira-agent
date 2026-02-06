"""Main entry point for JIRA Slack Agent."""

import asyncio
import logging
import sys
from typing import Optional

from .config import Settings, get_settings
from .db import DatabaseManager
from .dynamodb_client import DynamoDBClient
from .langbuilder_client import LangBuilderClient
from .slack_handler import SlackHandler

logger = logging.getLogger(__name__)

# Global references for cleanup
_db_manager: Optional[DatabaseManager] = None
_slack_handler: Optional[SlackHandler] = None


def setup_logging(level: str = "INFO") -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)


async def shutdown() -> None:
    """Cleanup on shutdown."""
    logger.info("Shutting down...")
    logger.info("Shutdown complete")


async def main() -> None:
    """Main application entry point."""
    global _db_manager, _slack_handler

    # Load settings
    try:
        settings = get_settings()
    except Exception as e:
        print(f"Failed to load settings: {e}")
        print("Make sure all required environment variables are set.")
        sys.exit(1)

    # Setup logging
    setup_logging(settings.log_level)
    logger.info("Starting JIRA Slack Agent...")

    # Ensure directories exist
    settings.ensure_data_directory()

    # Initialize database
    _db_manager = DatabaseManager(settings.database_path)
    await _db_manager.initialize()
    logger.info("Database initialized")

    # Initialize LangBuilder client
    # NOTE: LangBuilder handles ALL JIRA operations (read AND write)
    # Uses Run API with tweaks for ChatInput
    langbuilder_client = LangBuilderClient(
        flow_url=settings.langbuilder_flow_url,
        flow_id=settings.langbuilder_flow_id,
        api_key=settings.langbuilder_api_key,
        timeout=settings.request_timeout,
    )
    logger.info("LangBuilder client initialized (Run API)")

    # Initialize DynamoDB client for PM configurations
    dynamodb_client = DynamoDBClient(
        table_name=settings.dynamodb_table_name,
        region=settings.aws_region,
    )
    logger.info("DynamoDB client initialized (table=%s, region=%s)",
                settings.dynamodb_table_name, settings.aws_region)

    # Initialize Slack handler
    _slack_handler = SlackHandler(
        settings=settings,
        db_manager=_db_manager,
        langbuilder_client=langbuilder_client,
        dynamodb_client=dynamodb_client,
    )

    # Log configuration summary
    logger.info("=" * 50)
    logger.info("JIRA Slack Agent Configuration")
    logger.info("=" * 50)
    logger.info("LangBuilder Flow: %s", settings.langbuilder_flow_id)
    logger.info("Request Timeout: %ds", settings.request_timeout)
    logger.info("Mark Emoji: :%s:", settings.mark_emoji)
    logger.info("Database: %s", settings.database_path)
    logger.info("DynamoDB Table: %s (%s)", settings.dynamodb_table_name, settings.aws_region)
    logger.info("=" * 50)

    # Get initial stats
    stats = await _db_manager.get_stats()
    logger.info("Database stats: %s", stats)

    try:
        # Start Slack handler (blocks until shutdown)
        await _slack_handler.start()

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")

    finally:
        await shutdown()


def run() -> None:
    """Entry point for the application."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
