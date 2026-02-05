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
    ):
        self.settings = settings
        self.db = db_manager
        self.langbuilder = langbuilder_client

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
                await self._process_jira_sync(channel_id, user_id, client)
            finally:
                self._processing.discard(sync_key)

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

    async def _process_jira_sync(
        self,
        channel_id: str,
        user_id: str,
        client: AsyncWebClient,
    ) -> None:
        """Process a /jira-sync command."""
        logger.info("Processing /jira-sync for channel %s by user %s", channel_id, user_id)

        # Get unprocessed marked messages
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
        processing_msg = await client.chat_postMessage(
            channel=channel_id,
            text=f"Processing {len(marked_messages)} marked messages for JIRA sync...",
        )

        try:
            # Fetch full message content for messages
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
                "command": "/jira-sync",
                "messages": slack_messages,
            }

            # DEBUG: Log exact input being sent to LangBuilder
            logger.info("=" * 60)
            logger.info("LANGBUILDER INPUT DEBUG")
            logger.info("=" * 60)
            logger.info("Session ID: %s", session.uuid)
            logger.info("Input Data:\n%s", json.dumps(input_data, indent=2))
            logger.info("=" * 60)

            # Send to LangBuilder
            raw_response = await self.langbuilder.run_flow(
                session_id=session.uuid,
                input_data=input_data,
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
                    ticket_key=prop_data.get("ticket_key", "UNKNOWN"),
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
            proposed_display = proposal.proposed_value[:500]
            if len(proposal.proposed_value) > 500:
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
            logger.info("=" * 60)

            raw_response = await self.langbuilder.run_flow(
                session_id=session_uuid,
                input_data=input_data,
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
