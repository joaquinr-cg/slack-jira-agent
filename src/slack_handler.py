"""Slack event handler for JIRA Reviewer Agent."""

import json
import logging
from typing import Optional, Set

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from .config import Settings
from .db import (
    DatabaseManager,
    MarkedMessage,
    MarkType,
    Proposal,
    ProposalStatus,
    SessionStatus,
)
from .dynamodb_client import DynamoDBClient, build_tweaks_from_pm_config
from .langbuilder_client import (
    LangBuilderClient,
    LangBuilderError,
    LangBuilderTimeoutError,
    parse_llm_response,
)

logger = logging.getLogger(__name__)


class SlackHandler:
    """Handles all Slack interactions for the JIRA Reviewer Agent."""

    def __init__(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        langbuilder_client: LangBuilderClient,
        dynamodb_client: Optional[DynamoDBClient] = None,
    ):
        self.settings = settings
        self.db = db_manager
        self.langbuilder = langbuilder_client
        self.dynamodb = dynamodb_client

        # Deduplication tracking
        self._processing: Set[str] = set()

        # Initialize Slack app
        self.app = AsyncApp(token=settings.slack_bot_token)
        self._bot_user_id: Optional[str] = None

        # Register handlers
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register all Slack event handlers."""

        # ==========================================
        # REACTION HANDLERS (🎫 emoji)
        # ==========================================

        @self.app.event("reaction_added")
        async def handle_reaction_added(event: dict, client: AsyncWebClient) -> None:
            """Handle when a reaction is added to a message."""
            reaction = event.get("reaction", "")

            # Only handle our mark emoji
            if reaction != self.settings.mark_emoji:
                return

            channel_id = event["item"]["channel"]
            message_ts = event["item"]["ts"]
            user_id = event["user"]

            logger.info(
                "Reaction %s added to message %s by user %s",
                reaction,
                message_ts,
                user_id,
            )

            # Fetch the message content
            try:
                result = await client.conversations_history(
                    channel=channel_id,
                    latest=message_ts,
                    inclusive=True,
                    limit=1,
                )
                messages = result.get("messages", [])
                message_text = messages[0].get("text", "") if messages else None
                thread_ts = messages[0].get("thread_ts") if messages else None

            except Exception as e:
                logger.error("Failed to fetch message content: %s", str(e))
                message_text = None
                thread_ts = None

            # Store in database
            await self.db.mark_message(
                channel_id=channel_id,
                message_ts=message_ts,
                marked_by=user_id,
                mark_type=MarkType.EMOJI,
                thread_ts=thread_ts,
                message_text=message_text,
            )

            # Acknowledge with eyes emoji
            try:
                await client.reactions_add(
                    channel=channel_id,
                    timestamp=message_ts,
                    name=self.settings.pending_emoji,
                )
            except Exception as e:
                logger.debug("Could not add reaction: %s", str(e))

        @self.app.event("reaction_removed")
        async def handle_reaction_removed(event: dict, client: AsyncWebClient) -> None:
            """Handle when a reaction is removed from a message."""
            reaction = event.get("reaction", "")

            if reaction != self.settings.mark_emoji:
                return

            channel_id = event["item"]["channel"]
            message_ts = event["item"]["ts"]

            # Remove from database (only if not yet processed)
            removed = await self.db.unmark_message(channel_id, message_ts)

            if removed:
                # Remove our acknowledgment emoji
                try:
                    await client.reactions_remove(
                        channel=channel_id,
                        timestamp=message_ts,
                        name=self.settings.pending_emoji,
                    )
                except Exception:
                    pass

        # ==========================================
        # SLASH COMMAND HANDLERS
        # ==========================================

        @self.app.command("/jira-review")
        async def handle_jira_review(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-review command to mark a thread for review."""
            await ack()

            channel_id = command["channel_id"]
            user_id = command["user_id"]

            # Note: Slack doesn't provide thread_ts in slash commands directly
            # User should use emoji for specific messages or this marks the channel context
            await self.db.mark_message(
                channel_id=channel_id,
                message_ts=command.get("message_ts", command["channel_id"]),
                marked_by=user_id,
                mark_type=MarkType.COMMAND,
                thread_ts=command.get("thread_ts"),
            )

            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Marked for JIRA review. Run `/jira-sync` when ready to process all marked messages.",
            )

        @self.app.command("/jira-sync")
        async def handle_jira_sync(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-sync command to trigger processing."""
            await ack()

            channel_id = command["channel_id"]
            user_id = command["user_id"]
            command_text = (command.get("text") or "").strip().lower()

            # Parse --transcripts-only flag from command text
            transcripts_only_override = "transcripts-only" in command_text or "transcripts_only" in command_text

            # Check for deduplication
            sync_key = f"sync:{channel_id}:{user_id}"
            if sync_key in self._processing:
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="A sync is already in progress. Please wait.",
                )
                return

            self._processing.add(sync_key)

            try:
                await self._process_jira_sync(channel_id, user_id, client, transcripts_only_override)
            finally:
                self._processing.discard(sync_key)

        # ==========================================
        # INTERACTIVE COMPONENT HANDLERS (Buttons)
        # ==========================================

        # ==========================================
        # /jira-agent COMMAND (PM ONBOARDING & ADMIN)
        # ==========================================

        @self.app.command("/jira-agent")
        async def handle_jira_agent(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-agent command with subcommands."""
            await ack()

            user_id = command["user_id"]
            channel_id = command["channel_id"]
            trigger_id = command["trigger_id"]
            text = (command.get("text") or "").strip().lower()

            if not self.dynamodb:
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="DynamoDB is not configured. PM management is unavailable.",
                )
                return

            if text == "setup":
                await self._open_setup_modal(client, trigger_id, user_id)
            elif text == "config":
                await self._show_config(client, channel_id, user_id)
            elif text == "update jira":
                await self._open_update_jira_modal(client, trigger_id, user_id)
            elif text == "update gdrive":
                await self._open_update_gdrive_modal(client, trigger_id, user_id)
            elif text == "admin list":
                if not self.settings.is_admin(user_id):
                    await client.chat_postEphemeral(
                        channel=channel_id, user=user_id,
                        text="You don't have admin permissions.",
                    )
                    return
                await self._admin_list_pms(client, channel_id, user_id)
            elif text.startswith("admin disable "):
                if not self.settings.is_admin(user_id):
                    await client.chat_postEphemeral(
                        channel=channel_id, user=user_id,
                        text="You don't have admin permissions.",
                    )
                    return
                target_id = text.replace("admin disable ", "").strip()
                await self._admin_disable_pm(client, channel_id, user_id, target_id)
            elif text.startswith("admin enable "):
                if not self.settings.is_admin(user_id):
                    await client.chat_postEphemeral(
                        channel=channel_id, user=user_id,
                        text="You don't have admin permissions.",
                    )
                    return
                target_id = text.replace("admin enable ", "").strip()
                await self._admin_enable_pm(client, channel_id, user_id, target_id)
            elif text == "admin stats":
                if not self.settings.is_admin(user_id):
                    await client.chat_postEphemeral(
                        channel=channel_id, user=user_id,
                        text="You don't have admin permissions.",
                    )
                    return
                await self._admin_stats(client, channel_id, user_id)
            else:
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=(
                        "*Available commands:*\n"
                        "`/jira-agent setup` - Configure your JIRA & GDrive credentials\n"
                        "`/jira-agent config` - View your current configuration\n"
                        "`/jira-agent update jira` - Update JIRA credentials\n"
                        "`/jira-agent update gdrive` - Update Google Drive settings\n"
                        "`/jira-agent admin list` - List all PMs (admin)\n"
                        "`/jira-agent admin disable <slack_id>` - Disable a PM (admin)\n"
                        "`/jira-agent admin enable <slack_id>` - Enable a PM (admin)\n"
                        "`/jira-agent admin stats` - Usage statistics (admin)"
                    ),
                )

        # ==========================================
        # MODAL SUBMISSION HANDLERS
        # ==========================================

        @self.app.view("pm_setup_modal")
        async def handle_setup_submission(ack, body: dict, client: AsyncWebClient, view: dict) -> None:
            """Handle PM setup modal submission."""
            await ack()
            user_id = body["user"]["id"]
            values = view["state"]["values"]

            try:
                # Resolve secrets: use new value if provided, else keep existing
                existing_secrets = {}
                metadata = view.get("private_metadata", "")
                if metadata:
                    try:
                        existing_secrets = json.loads(metadata)
                    except json.JSONDecodeError:
                        pass

                jira_token = (
                    values["jira_token_block"]["jira_token_input"]["value"]
                    or existing_secrets.get("existing_jira_token", "")
                )
                gdrive_key = (
                    values["gdrive_key_block"]["gdrive_key_input"]["value"]
                    or existing_secrets.get("existing_gdrive_key", "")
                )

                pm_data = {
                    "slack_id": user_id,
                    "name": values["name_block"]["name_input"]["value"],
                    "email": values["email_block"]["email_input"]["value"],
                    "jira_config": {
                        "jira_url": values["jira_url_block"]["jira_url_input"]["value"],
                        "email": values["jira_email_block"]["jira_email_input"]["value"],
                        "api_token": jira_token,
                        "project_key": values["jira_project_block"]["jira_project_input"]["value"],
                        "auth_type": "basic",
                    },
                    "gdrive_config": {
                        "project_id": values["gdrive_project_block"]["gdrive_project_input"]["value"],
                        "client_email": values["gdrive_email_block"]["gdrive_email_input"]["value"],
                        "private_key": gdrive_key,
                        "folder_id": values["gdrive_folder_block"]["gdrive_folder_input"]["value"],
                        "folder_name": (values["gdrive_folder_name_block"]["gdrive_folder_name_input"]["value"] or ""),
                        "private_key_id": "",
                        "client_id": "",
                        "file_filter": "",
                    },
                    "flow_config": {
                        "transcripts_only": False,
                        "notification_channel": "",
                        "auto_approve": False,
                    },
                }
                await self.dynamodb.create_pm(pm_data)

                await client.chat_postMessage(
                    channel=user_id,
                    text="Your JIRA Agent configuration has been saved. You can now use `/jira-sync` to process messages.",
                )
            except Exception as e:
                logger.exception("Failed to save PM setup")
                await client.chat_postMessage(
                    channel=user_id,
                    text=f"Failed to save configuration: {str(e)}",
                )

        @self.app.view("pm_update_jira_modal")
        async def handle_update_jira_submission(ack, body: dict, client: AsyncWebClient, view: dict) -> None:
            """Handle JIRA update modal submission."""
            await ack()
            user_id = body["user"]["id"]
            values = view["state"]["values"]

            try:
                # Get current config to preserve fields not in the modal
                current = await self.dynamodb.get_pm_config(user_id)
                current_jira = current.get("jira_config", {}) if current else {}

                new_token = values["jira_token_block"]["jira_token_input"]["value"]
                jira_config = {
                    "jira_url": values["jira_url_block"]["jira_url_input"]["value"],
                    "email": values["jira_email_block"]["jira_email_input"]["value"],
                    "api_token": new_token if new_token else current_jira.get("api_token", ""),
                    "project_key": values["jira_project_block"]["jira_project_input"]["value"],
                    "auth_type": current_jira.get("auth_type", "basic"),
                }
                await self.dynamodb.update_pm(user_id, {"jira_config": jira_config})

                await client.chat_postMessage(
                    channel=user_id,
                    text="JIRA configuration updated.",
                )
            except Exception as e:
                logger.exception("Failed to update JIRA config")
                await client.chat_postMessage(
                    channel=user_id,
                    text=f"Failed to update JIRA config: {str(e)}",
                )

        @self.app.view("pm_update_gdrive_modal")
        async def handle_update_gdrive_submission(ack, body: dict, client: AsyncWebClient, view: dict) -> None:
            """Handle GDrive update modal submission."""
            await ack()
            user_id = body["user"]["id"]
            values = view["state"]["values"]

            try:
                current = await self.dynamodb.get_pm_config(user_id)
                current_gdrive = current.get("gdrive_config", {}) if current else {}

                new_key = values["gdrive_key_block"]["gdrive_key_input"]["value"]
                gdrive_config = {
                    "project_id": values["gdrive_project_block"]["gdrive_project_input"]["value"],
                    "client_email": values["gdrive_email_block"]["gdrive_email_input"]["value"],
                    "private_key": new_key if new_key else current_gdrive.get("private_key", ""),
                    "folder_id": values["gdrive_folder_block"]["gdrive_folder_input"]["value"],
                    "folder_name": (values["gdrive_folder_name_block"]["gdrive_folder_name_input"]["value"] or ""),
                    "private_key_id": current_gdrive.get("private_key_id", ""),
                    "client_id": current_gdrive.get("client_id", ""),
                    "file_filter": current_gdrive.get("file_filter", ""),
                }
                await self.dynamodb.update_pm(user_id, {"gdrive_config": gdrive_config})

                await client.chat_postMessage(
                    channel=user_id,
                    text="Google Drive configuration updated.",
                )
            except Exception as e:
                logger.exception("Failed to update GDrive config")
                await client.chat_postMessage(
                    channel=user_id,
                    text=f"Failed to update GDrive config: {str(e)}",
                )

        # ==========================================
        # INTERACTIVE COMPONENT HANDLERS (Buttons)
        # ==========================================

        @self.app.action("approve_proposal")
        async def handle_approve(ack, body: dict, client: AsyncWebClient) -> None:
            """Handle approve button click."""
            await ack()
            await self._handle_proposal_response(
                body, client, ProposalStatus.APPROVED
            )

        @self.app.action("reject_proposal")
        async def handle_reject(ack, body: dict, client: AsyncWebClient) -> None:
            """Handle reject button click."""
            await ack()
            await self._handle_proposal_response(
                body, client, ProposalStatus.REJECTED
            )

        @self.app.action("generate_from_transcript")
        async def handle_generate_from_transcript(ack, body: dict, client: AsyncWebClient) -> None:
            """Handle 'Generate Tickets from Latest Transcript' button click."""
            await ack()

            user_id = body["user"]["id"]
            channel_id = body["channel"]["id"]
            message_ts = body["message"]["ts"]

            # Replace button with confirmation text
            original_blocks = body["message"]["blocks"]
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Generating tickets from transcript...",
                        }
                    ],
                }
            )
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=updated_blocks,
                text="Generating tickets from transcript...",
            )

            # Trigger the main flow with transcripts_only
            await self._process_jira_sync(channel_id, user_id, client, transcripts_only_override=True)

    async def _process_jira_sync(
        self,
        channel_id: str,
        user_id: str,
        client: AsyncWebClient,
        transcripts_only_override: bool = False,
    ) -> None:
        """Process a /jira-sync command."""
        logger.info("Processing /jira-sync for channel %s by user %s", channel_id, user_id)

        # Fetch PM config from DynamoDB (if available)
        pm_config = None
        extra_tweaks = None
        transcripts_only = transcripts_only_override

        if self.dynamodb:
            pm_config = await self.dynamodb.get_pm_config(user_id)
            if pm_config:
                extra_tweaks = build_tweaks_from_pm_config(
                    pm_config,
                    default_gdrive=self._get_default_gdrive_config(),
                )
                # CLI override takes precedence, then DynamoDB flow_config
                if not transcripts_only:
                    transcripts_only = (
                        pm_config.get("flow_config", {}).get("transcripts_only", False)
                    )
                logger.info(
                    "PM config loaded for %s: transcripts_only=%s, tweaks_components=%s",
                    user_id,
                    transcripts_only,
                    list(extra_tweaks.keys()) if extra_tweaks else [],
                )
            else:
                logger.info("No PM config in DynamoDB for %s, using defaults", user_id)

        # Get unprocessed marked messages (skip if transcripts_only mode)
        marked_messages = []
        if not transcripts_only:
            marked_messages = await self.db.get_unprocessed_marked_messages(channel_id)

            if not marked_messages:
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=f"No messages marked for JIRA review. "
                    f"Add the :{self.settings.mark_emoji}: emoji to messages first.",
                )
                return

        # Create session
        session = await self.db.create_session(channel_id, user_id)

        # Send processing message
        if transcripts_only:
            processing_text = "Processing transcript-only JIRA sync..."
        else:
            processing_text = f"Processing {len(marked_messages)} marked messages for JIRA sync..."
        processing_msg = await client.chat_postMessage(
            channel=channel_id,
            text=processing_text,
        )

        try:
            # Fetch full message content for messages (empty list for transcripts_only)
            slack_messages = []
            if marked_messages:
                slack_messages = await self._fetch_message_contents(
                    marked_messages, client
                )
                # Mark messages as processed
                message_ids = [m.id for m in marked_messages if m.id]
                await self.db.mark_messages_as_processed(message_ids, session.uuid)

            # Update session status
            await self.db.update_session_status(session.uuid, SessionStatus.PROCESSING)

            # Prepare input for LangBuilder (simplified format)
            # session_id is passed separately via run_flow(), not inside the message
            # LangBuilder flow will handle enrichment (JIRA + GDrive) via its own tools
            input_data = {
                "command": "transcripts_only" if transcripts_only else "/jira-sync",
                "messages": slack_messages,
            }

            # DEBUG: Log exact input being sent to LangBuilder
            logger.info("=" * 60)
            logger.info("LANGBUILDER INPUT DEBUG")
            logger.info("=" * 60)
            logger.info("Session ID: %s", session.uuid)
            logger.info("Input Data:\n%s", json.dumps(input_data, indent=2))
            if extra_tweaks:
                # Log component names only (not secrets)
                logger.info("Extra tweaks components: %s", list(extra_tweaks.keys()))
            logger.info("=" * 60)

            # Send to LangBuilder
            raw_response = await self.langbuilder.run_flow(
                session_id=session.uuid,
                input_data=input_data,
                extra_tweaks=extra_tweaks,
            )

            # DEBUG: Log raw response from LangBuilder
            logger.info("=" * 60)
            logger.info("LANGBUILDER OUTPUT DEBUG")
            logger.info("=" * 60)
            logger.info("Raw Response:\n%s", json.dumps(raw_response, indent=2)[:2000])
            logger.info("=" * 60)

            # Parse response
            llm_response = parse_llm_response(raw_response)

            if llm_response.error:
                raise LangBuilderError(llm_response.error)

            if not llm_response.proposals:
                # No proposals found
                await client.chat_update(
                    channel=channel_id,
                    ts=processing_msg["ts"],
                    text=f"Analysis complete. {llm_response.analysis_summary}\n\n"
                    "No JIRA updates proposed.",
                )
                await self.db.update_session_status(
                    session.uuid, SessionStatus.COMPLETED
                )
                return

            # Create proposals in database
            proposals = []
            for prop_data in llm_response.proposals:
                proposal = Proposal(
                    session_uuid=session.uuid,
                    proposal_id=prop_data.get("proposal_id", f"prop-{len(proposals)+1}"),
                    ticket_key=prop_data.get("ticket_key") or "NEW",
                    ticket_summary=prop_data.get("ticket_summary"),
                    change_type=prop_data.get("change_type", "update"),
                    field_name=prop_data.get("field"),
                    current_value=prop_data.get("current_value"),
                    proposed_value=prop_data.get("proposed_value"),
                    source=prop_data.get("source"),
                    source_excerpt=prop_data.get("source_excerpt"),
                    confidence=prop_data.get("confidence", "medium"),
                )
                proposals.append(proposal)

            proposals = await self.db.create_proposals_batch(proposals)

            # Update session
            await self.db.update_session_counts(
                session.uuid, len(proposals), 0, 0
            )
            await self.db.update_session_status(
                session.uuid, SessionStatus.AWAITING_APPROVAL
            )

            # Update processing message with summary
            await client.chat_update(
                channel=channel_id,
                ts=processing_msg["ts"],
                text=f"Analysis complete. {llm_response.analysis_summary}\n\n"
                f"Found {len(proposals)} proposed JIRA updates. "
                "Review each proposal below:",
            )

            # Send approval messages for each proposal
            for proposal in proposals:
                message = await self._send_proposal_message(
                    client, channel_id, session.uuid, proposal
                )
                if message:
                    await self.db.update_proposal_slack_ts(
                        session.uuid, proposal.proposal_id, message["ts"]
                    )

        except LangBuilderTimeoutError:
            await client.chat_update(
                channel=channel_id,
                ts=processing_msg["ts"],
                text="The analysis is taking longer than expected. Please try again later.",
            )
            await self.db.update_session_status(
                session.uuid, SessionStatus.FAILED, "Timeout"
            )

        except LangBuilderError as e:
            await client.chat_update(
                channel=channel_id,
                ts=processing_msg["ts"],
                text=f"Error processing messages: {str(e)}",
            )
            await self.db.update_session_status(
                session.uuid, SessionStatus.FAILED, str(e)
            )

        except Exception as e:
            logger.exception("Unexpected error in jira-sync")
            await client.chat_update(
                channel=channel_id,
                ts=processing_msg["ts"],
                text="An unexpected error occurred. Please try again.",
            )
            await self.db.update_session_status(
                session.uuid, SessionStatus.FAILED, str(e)
            )

    async def _fetch_message_contents(
        self,
        marked_messages: list[MarkedMessage],
        client: AsyncWebClient,
    ) -> list[dict]:
        """Fetch full content for marked messages.

        Returns simplified format with only the text content.
        LangBuilder doesn't need Slack metadata (channel_id, timestamps, etc.)
        """
        slack_messages = []

        for msg in marked_messages:
            # If we already have the text, use it
            if msg.message_text:
                slack_messages.append({"text": msg.message_text})
                continue

            # Fetch from Slack
            try:
                # If it's a thread, get all messages in thread
                if msg.thread_ts:
                    result = await client.conversations_replies(
                        channel=msg.channel_id,
                        ts=msg.thread_ts,
                    )
                    messages = result.get("messages", [])
                    thread_text = "\n---\n".join(
                        [m.get("text", "") for m in messages]
                    )
                    slack_messages.append({"text": thread_text})
                else:
                    # Single message
                    result = await client.conversations_history(
                        channel=msg.channel_id,
                        latest=msg.message_ts,
                        inclusive=True,
                        limit=1,
                    )
                    messages = result.get("messages", [])
                    if messages:
                        slack_messages.append({"text": messages[0].get("text", "")})

            except Exception as e:
                logger.error(
                    "Failed to fetch message %s: %s", msg.message_ts, str(e)
                )

        return slack_messages

    async def _send_proposal_message(
        self,
        client: AsyncWebClient,
        channel_id: str,
        session_uuid: str,
        proposal: Proposal,
    ) -> Optional[dict]:
        """Send an approval message for a proposal."""
        # Build the message blocks
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"JIRA Update Proposal",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Ticket:*\n{proposal.ticket_key}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Change:*\n{proposal.change_type}",
                    },
                ],
            },
        ]

        # Add ticket summary if available
        if proposal.ticket_summary:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Summary:* {proposal.ticket_summary}",
                    },
                }
            )

        # Add field info if applicable
        if proposal.field_name:
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Field:*\n{proposal.field_name}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Confidence:*\n{proposal.confidence}",
                        },
                    ],
                }
            )

        # Current value
        if proposal.current_value:
            current_display = proposal.current_value[:500]
            if len(proposal.current_value) > 500:
                current_display += "..."
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Current:*\n```{current_display}```",
                    },
                }
            )

        # Proposed value
        if proposal.proposed_value:
            # Handle both string and dict values
            if isinstance(proposal.proposed_value, dict):
                import json
                proposed_str = json.dumps(proposal.proposed_value, indent=2)
            else:
                proposed_str = str(proposal.proposed_value)
            proposed_display = proposed_str[:500]
            if len(proposed_str) > 500:
                proposed_display += "..."
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Proposed:*\n```{proposed_display}```",
                    },
                }
            )

        # Source excerpt
        if proposal.source_excerpt:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_Source ({proposal.source}): {proposal.source_excerpt[:200]}_",
                        }
                    ],
                }
            )

        # Action buttons
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                        "style": "primary",
                        "action_id": "approve_proposal",
                        "value": json.dumps(
                            {
                                "session_uuid": session_uuid,
                                "proposal_id": proposal.proposal_id,
                            }
                        ),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                        "style": "danger",
                        "action_id": "reject_proposal",
                        "value": json.dumps(
                            {
                                "session_uuid": session_uuid,
                                "proposal_id": proposal.proposal_id,
                            }
                        ),
                    },
                ],
            }
        )

        try:
            result = await client.chat_postMessage(
                channel=channel_id,
                blocks=blocks,
                text=f"JIRA Update Proposal for {proposal.ticket_key}",
            )
            return result
        except Exception as e:
            logger.error("Failed to send proposal message: %s", str(e))
            return None

    async def _handle_proposal_response(
        self,
        body: dict,
        client: AsyncWebClient,
        status: ProposalStatus,
    ) -> None:
        """Handle approve/reject button click."""
        action = body["actions"][0]
        value = json.loads(action["value"])
        session_uuid = value["session_uuid"]
        proposal_id = value["proposal_id"]
        user_id = body["user"]["id"]
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]

        logger.info(
            "Proposal %s %s by user %s",
            proposal_id,
            status.value,
            user_id,
        )

        # Update proposal status in DB
        await self.db.update_proposal_status(
            session_uuid, proposal_id, status, user_id
        )

        # Update the Slack message to show the decision (remove buttons)
        status_emoji = (
            self.settings.approved_emoji
            if status == ProposalStatus.APPROVED
            else self.settings.rejected_emoji
        )
        status_text = "Approved" if status == ProposalStatus.APPROVED else "Rejected"

        original_blocks = body["message"]["blocks"]
        updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        updated_blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":{status_emoji}: *{status_text}* by <@{user_id}>",
                    }
                ],
            }
        )

        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=updated_blocks,
            text=f"Proposal {status_text}",
        )

        # Check if ALL proposals have been responded
        all_responded = await self.db.are_all_proposals_responded(session_uuid)

        if all_responded:
            await self._send_approval_decisions_to_llm(session_uuid, channel_id, client)

    async def _send_approval_decisions_to_llm(
        self,
        session_uuid: str,
        channel_id: str,
        client: AsyncWebClient,
    ) -> None:
        """Send all approval decisions to LangBuilder as a continuation."""
        all_proposals = await self.db.get_proposals_for_session(session_uuid)

        if not all_proposals:
            return

        # Fetch PM config tweaks for execution (needs JIRA credentials)
        extra_tweaks = None
        if self.dynamodb:
            session = await self.db.get_session(session_uuid)
            if session:
                pm_config = await self.dynamodb.get_pm_config(session.triggered_by)
                if pm_config:
                    extra_tweaks = build_tweaks_from_pm_config(
                        pm_config,
                        default_gdrive=self._get_default_gdrive_config(),
                    )

        # Build the decision summary for the LLM
        decisions = []
        for p in all_proposals:
            decisions.append({
                "proposal_id": p.proposal_id,
                "ticket_key": p.ticket_key,
                "change_type": p.change_type,
                "field_name": p.field_name,
                "proposed_value": p.proposed_value,
                "decision": p.status.value,  # "approved" or "rejected"
                "reviewed_by": p.reviewed_by,
            })

        approved_count = sum(1 for d in decisions if d["decision"] == "approved")
        rejected_count = sum(1 for d in decisions if d["decision"] == "rejected")

        # Send status message
        status_msg = await client.chat_postMessage(
            channel=channel_id,
            text=f"All proposals reviewed. Sending {approved_count} approved, {rejected_count} rejected to LangBuilder...",
        )

        try:
            # Send decisions to LangBuilder (same session_id for continuity)
            # session_id is passed separately via run_flow(), not inside the message
            input_data = {
                "command": "approval_decisions",
                "decisions": decisions,
            }

            # DEBUG: Log exact input being sent to LangBuilder
            logger.info("=" * 60)
            logger.info("LANGBUILDER INPUT DEBUG (Approval Decisions)")
            logger.info("=" * 60)
            logger.info("Session ID: %s", session_uuid)
            logger.info("Input Data:\n%s", json.dumps(input_data, indent=2))
            if extra_tweaks:
                logger.info("Extra tweaks components: %s", list(extra_tweaks.keys()))
            logger.info("=" * 60)

            raw_response = await self.langbuilder.run_flow(
                session_id=session_uuid,
                input_data=input_data,
                extra_tweaks=extra_tweaks,
            )

            # DEBUG: Log raw response from LangBuilder
            logger.info("=" * 60)
            logger.info("LANGBUILDER OUTPUT DEBUG (Approval Decisions)")
            logger.info("=" * 60)
            logger.info("Raw Response:\n%s", json.dumps(raw_response, indent=2)[:2000])
            logger.info("=" * 60)

            # Parse and display LLM response
            llm_response = parse_llm_response(raw_response)

            # Update status message with LLM's response
            response_text = llm_response.analysis_summary or "Processing complete."
            await client.chat_update(
                channel=channel_id,
                ts=status_msg["ts"],
                text=response_text,
            )

            # Mark session as completed
            await self.db.update_session_status(session_uuid, SessionStatus.COMPLETED)

        except LangBuilderTimeoutError:
            await client.chat_update(
                channel=channel_id,
                ts=status_msg["ts"],
                text="Request timed out. The LLM may still be processing.",
            )
            await self.db.update_session_status(
                session_uuid, SessionStatus.FAILED, "Timeout"
            )

        except LangBuilderError as e:
            await client.chat_update(
                channel=channel_id,
                ts=status_msg["ts"],
                text=f"Error: {str(e)}",
            )
            await self.db.update_session_status(
                session_uuid, SessionStatus.FAILED, str(e)
            )

        except Exception as e:
            logger.exception("Unexpected error sending decisions to LLM")
            await client.chat_update(
                channel=channel_id,
                ts=status_msg["ts"],
                text="An unexpected error occurred.",
            )
            await self.db.update_session_status(
                session_uuid, SessionStatus.FAILED, str(e)
            )

    def _get_default_gdrive_config(self) -> dict[str, str]:
        """Build default GDrive config from shared env settings."""
        return {
            "project_id": self.settings.gdrive_project_id,
            "client_email": self.settings.gdrive_client_email,
            "private_key": self.settings.gdrive_private_key,
            "private_key_id": self.settings.gdrive_private_key_id,
            "client_id": self.settings.gdrive_client_id,
            "folder_id": self.settings.gdrive_folder_id,
            "folder_name": self.settings.gdrive_folder_name,
            "file_filter": self.settings.gdrive_file_filter,
        }

    # ==========================================
    # PM ONBOARDING MODALS
    # ==========================================

    async def _open_setup_modal(
        self, client: AsyncWebClient, trigger_id: str, user_id: str
    ) -> None:
        """Open the full PM setup modal."""
        # Check if user already has a config
        existing = await self.dynamodb.get_pm_config(user_id)

        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "Basic Information"}},
            {
                "type": "input", "block_id": "name_block",
                "element": {
                    "type": "plain_text_input", "action_id": "name_input",
                    **({"initial_value": existing["name"]} if existing and existing.get("name") else {}),
                },
                "label": {"type": "plain_text", "text": "Your Name"},
            },
            {
                "type": "input", "block_id": "email_block",
                "element": {
                    "type": "plain_text_input", "action_id": "email_input",
                    **({"initial_value": existing["email"]} if existing and existing.get("email") else {}),
                },
                "label": {"type": "plain_text", "text": "Email"},
            },
            {"type": "divider"},
            {"type": "header", "text": {"type": "plain_text", "text": "JIRA Configuration"}},
            {
                "type": "input", "block_id": "jira_url_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_url_input",
                    "placeholder": {"type": "plain_text", "text": "https://company.atlassian.net"},
                    **({"initial_value": existing["jira_config"]["jira_url"]} if existing and existing.get("jira_config", {}).get("jira_url") else {}),
                },
                "label": {"type": "plain_text", "text": "JIRA URL"},
            },
            {
                "type": "input", "block_id": "jira_email_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_email_input",
                    "placeholder": {"type": "plain_text", "text": "you@company.com"},
                    **({"initial_value": existing["jira_config"]["email"]} if existing and existing.get("jira_config", {}).get("email") else {}),
                },
                "label": {"type": "plain_text", "text": "JIRA Email"},
            },
            {
                "type": "input", "block_id": "jira_token_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_token_input",
                    "placeholder": {"type": "plain_text", "text": "ATATT3x..." if not existing else "Leave empty to keep current"},
                },
                "label": {"type": "plain_text", "text": "JIRA API Token"},
                **({"optional": True} if existing else {}),
            },
            {
                "type": "input", "block_id": "jira_project_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_project_input",
                    "placeholder": {"type": "plain_text", "text": "LAN"},
                    **({"initial_value": existing["jira_config"]["project_key"]} if existing and existing.get("jira_config", {}).get("project_key") else {}),
                },
                "label": {"type": "plain_text", "text": "JIRA Project Key"},
            },
            {"type": "divider"},
            {"type": "header", "text": {"type": "plain_text", "text": "Google Drive Configuration"}},
            {
                "type": "input", "block_id": "gdrive_project_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_project_input",
                    "placeholder": {"type": "plain_text", "text": "my-gcp-project-123"},
                    **({"initial_value": existing["gdrive_config"]["project_id"]} if existing and existing.get("gdrive_config", {}).get("project_id") else {}),
                },
                "label": {"type": "plain_text", "text": "GCP Project ID"},
            },
            {
                "type": "input", "block_id": "gdrive_email_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_email_input",
                    "placeholder": {"type": "plain_text", "text": "sa@project.iam.gserviceaccount.com"},
                    **({"initial_value": existing["gdrive_config"]["client_email"]} if existing and existing.get("gdrive_config", {}).get("client_email") else {}),
                },
                "label": {"type": "plain_text", "text": "Service Account Email"},
            },
            {
                "type": "input", "block_id": "gdrive_key_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_key_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "-----BEGIN PRIVATE KEY-----\n..." if not existing else "Leave empty to keep current"},
                },
                "label": {"type": "plain_text", "text": "Service Account Private Key"},
                **({"optional": True} if existing else {}),
            },
            {
                "type": "input", "block_id": "gdrive_folder_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_folder_input",
                    "placeholder": {"type": "plain_text", "text": "1ABC123xyz"},
                    **({"initial_value": existing["gdrive_config"]["folder_id"]} if existing and existing.get("gdrive_config", {}).get("folder_id") else {}),
                },
                "label": {"type": "plain_text", "text": "Google Drive Folder ID"},
            },
            {
                "type": "input", "block_id": "gdrive_folder_name_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_folder_name_input",
                    "placeholder": {"type": "plain_text", "text": "Meet recordings"},
                    **({"initial_value": existing["gdrive_config"]["folder_name"]} if existing and existing.get("gdrive_config", {}).get("folder_name") else {}),
                },
                "label": {"type": "plain_text", "text": "Folder Name (optional fallback)"},
                "optional": True,
            },
        ]

        # If updating existing config, handle secrets
        if existing:
            jira_token = existing.get("jira_config", {}).get("api_token", "")
            gdrive_key = existing.get("gdrive_config", {}).get("private_key", "")

            # Store current secrets as private_metadata so submission can use them
            private_metadata = json.dumps({
                "existing_jira_token": jira_token,
                "existing_gdrive_key": gdrive_key,
            })
        else:
            private_metadata = ""

        view = {
            "type": "modal",
            "callback_id": "pm_setup_modal",
            "title": {"type": "plain_text", "text": "JIRA Agent Setup"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": private_metadata,
            "blocks": blocks,
        }

        try:
            await client.views_open(trigger_id=trigger_id, view=view)
        except Exception as e:
            logger.exception("Failed to open setup modal")

    async def _open_update_jira_modal(
        self, client: AsyncWebClient, trigger_id: str, user_id: str
    ) -> None:
        """Open modal to update JIRA credentials."""
        existing = await self.dynamodb.get_pm_config(user_id)
        jira = existing.get("jira_config", {}) if existing else {}

        blocks = [
            {
                "type": "input", "block_id": "jira_url_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_url_input",
                    **({"initial_value": jira["jira_url"]} if jira.get("jira_url") else {}),
                },
                "label": {"type": "plain_text", "text": "JIRA URL"},
            },
            {
                "type": "input", "block_id": "jira_email_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_email_input",
                    **({"initial_value": jira["email"]} if jira.get("email") else {}),
                },
                "label": {"type": "plain_text", "text": "JIRA Email"},
            },
            {
                "type": "input", "block_id": "jira_token_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_token_input",
                    "placeholder": {"type": "plain_text", "text": "Leave empty to keep current token"},
                },
                "label": {"type": "plain_text", "text": "JIRA API Token"},
                "optional": True,
            },
            {
                "type": "input", "block_id": "jira_project_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_project_input",
                    **({"initial_value": jira["project_key"]} if jira.get("project_key") else {}),
                },
                "label": {"type": "plain_text", "text": "JIRA Project Key"},
            },
        ]

        view = {
            "type": "modal",
            "callback_id": "pm_update_jira_modal",
            "title": {"type": "plain_text", "text": "Update JIRA Config"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": blocks,
        }

        try:
            await client.views_open(trigger_id=trigger_id, view=view)
        except Exception as e:
            logger.exception("Failed to open JIRA update modal")

    async def _open_update_gdrive_modal(
        self, client: AsyncWebClient, trigger_id: str, user_id: str
    ) -> None:
        """Open modal to update Google Drive settings."""
        existing = await self.dynamodb.get_pm_config(user_id)
        gdrive = existing.get("gdrive_config", {}) if existing else {}

        blocks = [
            {
                "type": "input", "block_id": "gdrive_project_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_project_input",
                    **({"initial_value": gdrive["project_id"]} if gdrive.get("project_id") else {}),
                },
                "label": {"type": "plain_text", "text": "GCP Project ID"},
            },
            {
                "type": "input", "block_id": "gdrive_email_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_email_input",
                    **({"initial_value": gdrive["client_email"]} if gdrive.get("client_email") else {}),
                },
                "label": {"type": "plain_text", "text": "Service Account Email"},
            },
            {
                "type": "input", "block_id": "gdrive_key_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_key_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Leave empty to keep current key"},
                },
                "label": {"type": "plain_text", "text": "Service Account Private Key"},
                "optional": True,
            },
            {
                "type": "input", "block_id": "gdrive_folder_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_folder_input",
                    **({"initial_value": gdrive["folder_id"]} if gdrive.get("folder_id") else {}),
                },
                "label": {"type": "plain_text", "text": "Google Drive Folder ID"},
            },
            {
                "type": "input", "block_id": "gdrive_folder_name_block",
                "element": {
                    "type": "plain_text_input", "action_id": "gdrive_folder_name_input",
                    **({"initial_value": gdrive["folder_name"]} if gdrive.get("folder_name") else {}),
                },
                "label": {"type": "plain_text", "text": "Folder Name (optional)"},
                "optional": True,
            },
        ]

        view = {
            "type": "modal",
            "callback_id": "pm_update_gdrive_modal",
            "title": {"type": "plain_text", "text": "Update GDrive Config"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": blocks,
        }

        try:
            await client.views_open(trigger_id=trigger_id, view=view)
        except Exception as e:
            logger.exception("Failed to open GDrive update modal")

    # ==========================================
    # CONFIG DISPLAY
    # ==========================================

    async def _show_config(
        self, client: AsyncWebClient, channel_id: str, user_id: str
    ) -> None:
        """Show the user's current PM configuration."""
        config = await self.dynamodb.get_pm_config(user_id)
        if not config:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text="No configuration found. Run `/jira-agent setup` to get started.",
            )
            return

        jira = config.get("jira_config", {})
        gdrive = config.get("gdrive_config", {})
        flow = config.get("flow_config", {})
        last = config.get("last_processed_transcript", {})

        # Mask sensitive values
        token_masked = (jira.get("api_token", "")[:8] + "...") if jira.get("api_token") else "Not set"
        key_masked = "Configured" if gdrive.get("private_key") else "Not set"

        text = (
            f"*Your JIRA Agent Configuration*\n\n"
            f"*Name:* {config.get('name', 'N/A')}\n"
            f"*Email:* {config.get('email', 'N/A')}\n"
            f"*Enabled:* {config.get('enabled', False)}\n\n"
            f"*JIRA:*\n"
            f"  URL: `{jira.get('jira_url', 'N/A')}`\n"
            f"  Email: `{jira.get('email', 'N/A')}`\n"
            f"  API Token: `{token_masked}`\n"
            f"  Project: `{jira.get('project_key', 'N/A')}`\n\n"
            f"*Google Drive:*\n"
            f"  Project ID: `{gdrive.get('project_id', 'N/A')}`\n"
            f"  Service Account: `{gdrive.get('client_email', 'N/A')}`\n"
            f"  Private Key: `{key_masked}`\n"
            f"  Folder ID: `{gdrive.get('folder_id', 'N/A')}`\n"
            f"  Folder Name: `{gdrive.get('folder_name', 'N/A')}`\n\n"
            f"*Flow Config:*\n"
            f"  Transcripts Only: `{flow.get('transcripts_only', False)}`\n"
            f"  Auto Approve: `{flow.get('auto_approve', False)}`\n\n"
            f"*Last Processed Transcript:*\n"
            f"  File: `{last.get('file_name', 'None')}`\n"
            f"  Processed At: `{last.get('processed_at', 'Never')}`"
        )

        await client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)

    # ==========================================
    # ADMIN COMMANDS
    # ==========================================

    async def _admin_list_pms(
        self, client: AsyncWebClient, channel_id: str, user_id: str
    ) -> None:
        """List all configured PMs."""
        try:
            pms = await self.dynamodb.list_enabled_pms()
        except Exception as e:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"Failed to fetch PM list: {str(e)}",
            )
            return

        if not pms:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text="No PMs configured.",
            )
            return

        lines = ["*Configured PMs:*\n"]
        for pm in pms:
            enabled_icon = "ON" if pm.get("enabled", False) else "OFF"
            last = pm.get("last_processed_transcript", {})
            last_processed = last.get("processed_at", "Never") if last.get("processed_at") else "Never"
            lines.append(
                f"  <@{pm['slack_id']}> | {pm.get('name', 'N/A')} | "
                f"`{enabled_icon}` | Project: `{pm.get('jira_config', {}).get('project_key', 'N/A')}` | "
                f"Last sync: `{last_processed}`"
            )

        await client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="\n".join(lines),
        )

    async def _admin_disable_pm(
        self, client: AsyncWebClient, channel_id: str, user_id: str, target_id: str
    ) -> None:
        """Disable a PM by Slack ID."""
        try:
            await self.dynamodb.disable_pm(target_id)
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"PM <@{target_id}> has been disabled.",
            )
        except Exception as e:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"Failed to disable PM: {str(e)}",
            )

    async def _admin_enable_pm(
        self, client: AsyncWebClient, channel_id: str, user_id: str, target_id: str
    ) -> None:
        """Enable a PM by Slack ID."""
        try:
            await self.dynamodb.enable_pm(target_id)
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"PM <@{target_id}> has been enabled.",
            )
        except Exception as e:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"Failed to enable PM: {str(e)}",
            )

    async def _admin_stats(
        self, client: AsyncWebClient, channel_id: str, user_id: str
    ) -> None:
        """Show admin usage statistics."""
        try:
            pms = await self.dynamodb.list_enabled_pms()
            db_stats = await self.db.get_stats()
        except Exception as e:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"Failed to fetch stats: {str(e)}",
            )
            return

        enabled_count = len(pms)
        text = (
            f"*JIRA Agent Statistics*\n\n"
            f"*PMs:*\n"
            f"  Enabled: `{enabled_count}`\n\n"
            f"*Sessions:*\n"
            f"  Total: `{db_stats.get('total_sessions', 0)}`\n"
            f"  Completed: `{db_stats.get('completed_sessions', 0)}`\n\n"
            f"*Proposals:*\n"
            f"  Total: `{db_stats.get('total_proposals', 0)}`\n"
            f"  Executed: `{db_stats.get('executed_proposals', 0)}`\n\n"
            f"*Pending:*\n"
            f"  Marked messages: `{db_stats.get('pending_marked_messages', 0)}`"
        )

        await client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)

    async def start(self) -> None:
        """Start the Slack handler."""
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        logger.info("Starting Slack handler in Socket Mode...")

        handler = AsyncSocketModeHandler(self.app, self.settings.slack_app_token)
        await handler.start_async()

    async def get_bot_user_id(self, client: AsyncWebClient) -> str:
        """Get the bot's user ID."""
        if self._bot_user_id is None:
            response = await client.auth_test()
            self._bot_user_id = response["user_id"]
        return self._bot_user_id
