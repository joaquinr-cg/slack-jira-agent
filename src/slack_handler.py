"""Slack event handler for JIRA Reviewer Agent."""

import asyncio
import json
import logging
import re
import urllib.request
from typing import Optional, Set

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from .config import Settings
from .cron_helper import natural_language_to_cron
from .file_extractor import extract_text_from_bytes
from .db import (
    AuditEntry,
    AuditEventType,
    DatabaseManager,
    MarkedMessage,
    MarkType,
    Proposal,
    ProposalStatus,
    SessionStatus,
)
from .dynamodb_client import (
    CHAT_COMPONENT_ID_JIRA,
    CHAT_COMPONENT_ID_JIRA_READER_WRITER,
    COMPONENT_ID_JIRA_READER_WRITER,
    COMPONENT_ID_JIRA_STATE_FETCHER,
    DynamoDBClient,
    build_tweaks_from_pm_config,
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
        dynamodb_client: Optional[DynamoDBClient] = None,
    ):
        self.settings = settings
        self.db = db_manager
        self.langbuilder = langbuilder_client
        self.dynamodb = dynamodb_client

        # Deduplication tracking
        self._processing: Set[str] = set()
        self._scheduler = None

        # Initialize Slack app
        self.app = AsyncApp(token=settings.slack_bot_token)
        self._bot_user_id: Optional[str] = None

        # Register handlers
        self._register_handlers()

    def set_scheduler(self, scheduler) -> None:
        """Set the transcript scheduler for manual trigger support."""
        self._scheduler = scheduler

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
        # @MENTION HANDLER (conversational JIRA chat)
        # ==========================================

        @self.app.event("app_mention")
        async def handle_app_mention(event: dict, client: AsyncWebClient) -> None:
            """Handle @bot mentions – route to the conversational chat flow."""
            channel_id = event["channel"]
            user_id = event["user"]
            text = event.get("text", "")
            thread_ts = event.get("thread_ts") or event["ts"]

            # Strip the bot mention from the text
            bot_user_id = await self.get_bot_user_id(client)
            clean_text = re.sub(rf"<@{bot_user_id}>", "", text).strip()

            # Download uploaded files, extract text, and append to message
            files = event.get("files", [])
            if files:
                logger.info("Slack file objects: %s", json.dumps(files, indent=2, default=str))
                file_texts = []
                for f in files:
                    url = f.get("url_private_download") or f.get("url_private", "")
                    name = f.get("name", "unknown")
                    mime = f.get("mimetype", "application/octet-stream")
                    if not url:
                        file_texts.append(f"[Attached file: {name} — no download URL]")
                        continue
                    try:
                        # Use urllib with a custom redirect handler that
                        # preserves the Authorization header on every hop.
                        def _download(dl_url: str, token: str) -> bytes:
                            class _AuthRedirectHandler(urllib.request.HTTPRedirectHandler):
                                def redirect_request(self, req, fp, code, msg, headers, newurl):
                                    new_req = super().redirect_request(
                                        req, fp, code, msg, headers, newurl,
                                    )
                                    if new_req is not None:
                                        new_req.add_unredirected_header(
                                            "Authorization", f"Bearer {token}",
                                        )
                                    return new_req

                            opener = urllib.request.build_opener(_AuthRedirectHandler)
                            req = urllib.request.Request(dl_url)
                            req.add_header("Authorization", f"Bearer {token}")
                            with opener.open(req, timeout=30) as resp:
                                return resp.read()

                        raw = await asyncio.to_thread(
                            _download, url, self.settings.slack_bot_token,
                        )
                        logger.info("Downloaded file %s: %d bytes (first 20: %s)", name, len(raw), raw[:20])
                        extracted = extract_text_from_bytes(raw, mime, name)
                        file_texts.append(f"--- File: {name} ---\n{extracted}")
                    except Exception as e:
                        logger.error("Error downloading file %s: %s", name, str(e))
                        file_texts.append(f"[Attached file: {name} — error: {str(e)[:100]}]")

                file_block = "\n\n".join(file_texts)
                clean_text = f"{clean_text}\n\n{file_block}" if clean_text else file_block

            if not clean_text:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="Hi! Tag me with a message to check or update JIRA tickets. For example: `@PM Buddy what's the status of LAN-92?`",
                )
                return

            if not self.settings.langbuilder_chat_flow_id:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="Chat flow is not configured. Set `LANGBUILDER_CHAT_FLOW_ID` to enable @mention conversations.",
                )
                return

            # Build JIRA credential tweaks from PM config (if available)
            chat_tweaks: dict = {}
            if self.dynamodb:
                pm_config = await self.dynamodb.get_pm_config(user_id)
                if pm_config:
                    full_tweaks = build_tweaks_from_pm_config(
                        pm_config,
                        shared_jira=self._get_shared_jira_config(),
                    )
                    # Map main-flow component IDs → chat-flow component IDs
                    jira_creds = full_tweaks.get(COMPONENT_ID_JIRA_READER_WRITER, {})
                    if jira_creds:
                        chat_tweaks[CHAT_COMPONENT_ID_JIRA] = jira_creds
                        chat_tweaks[CHAT_COMPONENT_ID_JIRA_READER_WRITER] = jira_creds
                elif self._get_shared_jira_config():
                    # No PM config but shared JIRA is available
                    shared = self._get_shared_jira_config()
                    jira_creds = {
                        "jira_url": shared.get("jira_url", ""),
                        "email": shared.get("email", ""),
                        "api_token": shared.get("api_token", ""),
                        "auth_type": "basic",
                    }
                    chat_tweaks[CHAT_COMPONENT_ID_JIRA] = jira_creds
                    chat_tweaks[CHAT_COMPONENT_ID_JIRA_READER_WRITER] = jira_creds

            # Use thread_ts as session_id so threaded replies share context
            session_id = f"slack-chat-{thread_ts}"

            # Show a thinking indicator
            thinking_msg = await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":hourglass_flowing_sand: Thinking…",
            )

            try:
                reply = await self.langbuilder.run_chat(
                    flow_id=self.settings.langbuilder_chat_flow_id,
                    chat_input_id=self.settings.langbuilder_chat_input_id,
                    session_id=session_id,
                    message=clean_text,
                    extra_tweaks=chat_tweaks,
                )

                # Replace the thinking message with the actual reply
                await client.chat_update(
                    channel=channel_id,
                    ts=thinking_msg["ts"],
                    text=reply,
                )
            except Exception as e:
                logger.exception("Chat flow error for user %s", user_id)
                await client.chat_update(
                    channel=channel_id,
                    ts=thinking_msg["ts"],
                    text=f"Sorry, something went wrong: {str(e)[:200]}",
                )

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

            # Parse --project=KEY flag for project filtering
            project_filter = None
            for part in command_text.split():
                if part.startswith("--project="):
                    project_filter = part.split("=", 1)[1].strip().upper()

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
                await self._process_jira_sync(
                    channel_id, user_id, client, transcripts_only_override,
                    project_filter=project_filter,
                )
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
            elif text == "check-transcripts":
                await self._manual_check_transcripts(client, channel_id, user_id)
            elif text == "schedule":
                await self._open_schedule_modal(client, trigger_id, user_id)
            elif text == "admin stats":
                if not self.settings.is_admin(user_id):
                    await client.chat_postEphemeral(
                        channel=channel_id, user=user_id,
                        text="You don't have admin permissions.",
                    )
                    return
                await self._admin_stats(client, channel_id, user_id)
            elif text.startswith("admin audit"):
                if not self.settings.is_admin(user_id):
                    await client.chat_postEphemeral(
                        channel=channel_id, user=user_id,
                        text="You don't have admin permissions.",
                    )
                    return
                audit_session = text.replace("admin audit", "").strip()
                await self._admin_audit_log(client, channel_id, user_id, audit_session or None)
            else:
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=(
                        "*Available commands:*\n\n"
                        "*/jira-setup*\nConfigure your JIRA & GDrive credentials\n\n"
                        "*/jira-config*\nView your current configuration\n\n"
                        "*/jira-update* `jira` or `gdrive`\nUpdate JIRA or GDrive credentials\n\n"
                        "*/jira-schedule*\nConfigure recurring sync schedule\n\n"
                        "*/jira-transcripts*\nManually check for new transcripts\n\n"
                        "*/jira-admin* `list|disable|enable|stats|audit`\nAdmin: manage PMs, stats, audit"
                    ),
                )

        # ==========================================
        # DEDICATED SLASH COMMANDS
        # ==========================================

        @self.app.command("/jira-setup")
        async def handle_jira_setup(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-setup – open PM onboarding modal."""
            await ack()
            user_id = command["user_id"]
            channel_id = command["channel_id"]
            trigger_id = command["trigger_id"]

            if not self.dynamodb:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="DynamoDB is not configured. PM management is unavailable.",
                )
                return
            await self._open_setup_modal(client, trigger_id, user_id)

        @self.app.command("/jira-config")
        async def handle_jira_config(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-config – show current PM configuration."""
            await ack()
            user_id = command["user_id"]
            channel_id = command["channel_id"]

            if not self.dynamodb:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="DynamoDB is not configured. PM management is unavailable.",
                )
                return
            await self._show_config(client, channel_id, user_id)

        @self.app.command("/jira-update")
        async def handle_jira_update(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-update jira|gdrive – update credentials."""
            await ack()
            user_id = command["user_id"]
            channel_id = command["channel_id"]
            trigger_id = command["trigger_id"]
            text = (command.get("text") or "").strip().lower()

            if not self.dynamodb:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="DynamoDB is not configured. PM management is unavailable.",
                )
                return

            if text == "jira":
                await self._open_update_jira_modal(client, trigger_id, user_id)
            elif text == "gdrive":
                await self._open_update_gdrive_modal(client, trigger_id, user_id)
            else:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="Usage: `/jira-update jira` or `/jira-update gdrive`",
                )

        @self.app.command("/jira-schedule")
        async def handle_jira_schedule(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-schedule – open schedule configuration modal."""
            await ack()
            user_id = command["user_id"]
            channel_id = command["channel_id"]
            trigger_id = command["trigger_id"]

            if not self.dynamodb:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="DynamoDB is not configured. PM management is unavailable.",
                )
                return
            await self._open_schedule_modal(client, trigger_id, user_id)

        @self.app.command("/jira-transcripts")
        async def handle_jira_transcripts(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-transcripts – manually check for new transcripts."""
            await ack()
            user_id = command["user_id"]
            channel_id = command["channel_id"]

            if not self.dynamodb:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="DynamoDB is not configured. PM management is unavailable.",
                )
                return
            await self._manual_check_transcripts(client, channel_id, user_id)

        @self.app.command("/jira-admin")
        async def handle_jira_admin(ack, command: dict, client: AsyncWebClient) -> None:
            """Handle /jira-admin – admin PM management commands."""
            await ack()
            user_id = command["user_id"]
            channel_id = command["channel_id"]
            text = (command.get("text") or "").strip().lower()

            if not self.dynamodb:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="DynamoDB is not configured. PM management is unavailable.",
                )
                return

            if not self.settings.is_admin(user_id):
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="You don't have admin permissions.",
                )
                return

            if text == "list":
                await self._admin_list_pms(client, channel_id, user_id)
            elif text.startswith("disable "):
                target_id = text.replace("disable ", "", 1).strip()
                await self._admin_disable_pm(client, channel_id, user_id, target_id)
            elif text.startswith("enable "):
                target_id = text.replace("enable ", "", 1).strip()
                await self._admin_enable_pm(client, channel_id, user_id, target_id)
            elif text == "stats":
                await self._admin_stats(client, channel_id, user_id)
            elif text.startswith("audit"):
                audit_session = text.replace("audit", "", 1).strip()
                await self._admin_audit_log(client, channel_id, user_id, audit_session or None)
            else:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text=(
                        "*Admin commands:*\n\n"
                        "*/jira-admin list*\nList all PMs\n\n"
                        "*/jira-admin disable* _<slack_id>_\nDisable a PM\n\n"
                        "*/jira-admin enable* _<slack_id>_\nEnable a PM\n\n"
                        "*/jira-admin stats*\nUsage statistics\n\n"
                        "*/jira-admin audit*\nView full audit log\n\n"
                        "*/jira-admin audit* _<session_uuid>_\nAudit log for a specific session"
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
                # Resolve secrets: use new value if provided, else keep existing from DynamoDB
                is_update = False
                metadata = view.get("private_metadata", "")
                if metadata:
                    try:
                        is_update = json.loads(metadata).get("is_update", False)
                    except json.JSONDecodeError:
                        pass

                existing_secrets = {}
                if is_update:
                    existing_config = await self.dynamodb.get_pm_config(user_id)
                    if existing_config:
                        existing_secrets = {
                            "existing_jira_token": existing_config.get("jira_config", {}).get("api_token", ""),
                        }

                # Handle JIRA token (may be absent when using shared service account)
                shared_jira_configured = bool(self.settings.jira_shared_api_token)
                if shared_jira_configured:
                    jira_token = ""
                    jira_url = ""
                    jira_email = ""
                else:
                    jira_token = (
                        values["jira_token_block"]["jira_token_input"]["value"]
                        or existing_secrets.get("existing_jira_token", "")
                    )
                    jira_url = values["jira_url_block"]["jira_url_input"]["value"]
                    jira_email = values["jira_email_block"]["jira_email_input"]["value"]

                pm_data = {
                    "slack_id": user_id,
                    "name": values["name_block"]["name_input"]["value"],
                    "email": values["email_block"]["email_input"]["value"],
                    "jira_config": {
                        "jira_url": jira_url,
                        "email": jira_email,
                        "api_token": jira_token,
                        "project_keys": [k.strip() for k in values["jira_project_block"]["jira_project_input"]["value"].split(",") if k.strip()],
                        "auth_type": "basic",
                    },
                    "gdrive_config": {
                        "folder_id": (values["gdrive_folder_block"]["gdrive_folder_input"]["value"] or ""),
                        "folder_name": (values["gdrive_folder_name_block"]["gdrive_folder_name_input"]["value"] or ""),
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
                    "project_keys": [k.strip() for k in values["jira_project_block"]["jira_project_input"]["value"].split(",") if k.strip()],
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

                gdrive_config = {
                    **current_gdrive,
                    "folder_id": values["gdrive_folder_block"]["gdrive_folder_input"]["value"],
                    "folder_name": (values["gdrive_folder_name_block"]["gdrive_folder_name_input"]["value"] or ""),
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

        @self.app.view("edit_proposal_modal")
        async def handle_edit_submission(ack, body: dict, client: AsyncWebClient, view: dict) -> None:
            """Handle edit proposal modal submission."""
            await ack()
            await self._handle_edit_submission(body, client, view)

        @self.app.view("rejection_reason_modal")
        async def handle_rejection_reason(ack, body: dict, client: AsyncWebClient, view: dict) -> None:
            """Handle rejection reason modal submission."""
            await ack()
            await self._handle_rejection_reason_submission(body, client, view)

        @self.app.view("schedule_config_modal")
        async def handle_schedule_submission(ack, body: dict, client: AsyncWebClient, view: dict) -> None:
            """Handle schedule config modal submission."""
            await ack()
            await self._handle_schedule_submission(body, client, view)

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
            """Handle reject button click - opens reason modal."""
            await ack()
            await self._open_rejection_reason_modal(body, client)

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

        @self.app.action("edit_proposal")
        async def handle_edit_proposal(ack, body: dict, client: AsyncWebClient) -> None:
            """Handle Edit & Approve button click - opens edit modal."""
            await ack()
            await self._open_edit_modal(body, client)

        @self.app.action("bulk_approve")
        async def handle_bulk_approve(ack, body: dict, client: AsyncWebClient) -> None:
            """Handle Approve All button click."""
            await ack()
            await self._handle_bulk_action(body, client, ProposalStatus.APPROVED)

        @self.app.action("bulk_reject")
        async def handle_bulk_reject(ack, body: dict, client: AsyncWebClient) -> None:
            """Handle Reject All button click."""
            await ack()
            await self._handle_bulk_action(body, client, ProposalStatus.REJECTED)

    async def _process_jira_sync(
        self,
        channel_id: str,
        user_id: str,
        client: AsyncWebClient,
        transcripts_only_override: bool = False,
        project_filter: Optional[str] = None,
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
                    shared_jira=self._get_shared_jira_config(),
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
                # Still pass shared JIRA creds so components can read/write
                shared_jira = self._get_shared_jira_config()
                if shared_jira:
                    extra_tweaks = {
                        COMPONENT_ID_JIRA_READER_WRITER: {**shared_jira, "auth_type": "basic"},
                        COMPONENT_ID_JIRA_STATE_FETCHER: {**shared_jira, "auth_type": "basic"},
                    }

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

        # Audit: sync triggered
        await self.db.append_audit_log(AuditEntry(
            event_type=AuditEventType.SYNC_TRIGGERED,
            user_id=user_id,
            session_uuid=session.uuid,
            metadata={"channel_id": channel_id, "transcripts_only": transcripts_only},
        ))

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

            # Add project filter if specified
            if project_filter:
                input_data["project_filter"] = project_filter

            # Feed rejection patterns to improve future proposal quality
            try:
                rejection_summary = await self.db.get_rejection_summary()
                if rejection_summary:
                    input_data["rejection_patterns"] = rejection_summary
            except Exception as e:
                logger.warning("Failed to fetch rejection patterns: %s", e)

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
                await self.db.append_audit_log(AuditEntry(
                    event_type=AuditEventType.SYNC_COMPLETED,
                    user_id=user_id,
                    session_uuid=session.uuid,
                    metadata={"proposals_count": 0},
                ))
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

            # Audit: proposals created
            for p in proposals:
                await self.db.append_audit_log(AuditEntry(
                    event_type=AuditEventType.PROPOSAL_CREATED,
                    user_id=user_id,
                    session_uuid=session.uuid,
                    proposal_id=p.proposal_id,
                    after_snapshot={
                        "ticket_key": p.ticket_key,
                        "change_type": p.change_type,
                        "field_name": p.field_name,
                        "proposed_value": p.proposed_value,
                        "confidence": p.confidence,
                    },
                ))

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

            # Post bulk actions summary message
            if len(proposals) > 1:
                summary_msg = await self._send_bulk_actions_message(
                    client, channel_id, session.uuid, len(proposals)
                )
                if summary_msg:
                    await self.db.update_session_summary_ts(
                        session.uuid, summary_msg["ts"]
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
            await self.db.append_audit_log(AuditEntry(
                event_type=AuditEventType.SYNC_FAILED,
                user_id=user_id,
                session_uuid=session.uuid,
                metadata={"error": "Timeout"},
            ))

        except LangBuilderError as e:
            await client.chat_update(
                channel=channel_id,
                ts=processing_msg["ts"],
                text=f"Error processing messages: {str(e)}",
            )
            await self.db.update_session_status(
                session.uuid, SessionStatus.FAILED, str(e)
            )
            await self.db.append_audit_log(AuditEntry(
                event_type=AuditEventType.SYNC_FAILED,
                user_id=user_id,
                session_uuid=session.uuid,
                metadata={"error": str(e)},
            ))

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
            await self.db.append_audit_log(AuditEntry(
                event_type=AuditEventType.SYNC_FAILED,
                user_id=user_id,
                session_uuid=session.uuid,
                metadata={"error": str(e)},
            ))

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
        button_value = json.dumps({
            "session_uuid": session_uuid,
            "proposal_id": proposal.proposal_id,
        })
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                        "style": "primary",
                        "action_id": "approve_proposal",
                        "value": button_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Edit & Approve", "emoji": True},
                        "action_id": "edit_proposal",
                        "value": button_value,
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

    async def _send_bulk_actions_message(
        self,
        client: AsyncWebClient,
        channel_id: str,
        session_uuid: str,
        total_proposals: int,
    ) -> Optional[dict]:
        """Send a summary message with Approve All / Reject All buttons."""
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Session Summary:* {total_proposals} proposals above.\n"
                    "Use the buttons below to approve or reject all remaining proposals at once.",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve All Remaining", "emoji": True},
                        "style": "primary",
                        "action_id": "bulk_approve",
                        "value": json.dumps({"session_uuid": session_uuid}),
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Confirm Bulk Approve"},
                            "text": {"type": "mrkdwn", "text": "Approve all pending proposals in this session?"},
                            "confirm": {"type": "plain_text", "text": "Yes, Approve All"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject All Remaining", "emoji": True},
                        "style": "danger",
                        "action_id": "bulk_reject",
                        "value": json.dumps({"session_uuid": session_uuid}),
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Confirm Bulk Reject"},
                            "text": {"type": "mrkdwn", "text": "Reject all pending proposals in this session?"},
                            "confirm": {"type": "plain_text", "text": "Yes, Reject All"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    },
                ],
            },
        ]

        try:
            return await client.chat_postMessage(
                channel=channel_id,
                blocks=blocks,
                text=f"Bulk actions for {total_proposals} proposals",
            )
        except Exception as e:
            logger.error("Failed to send bulk actions message: %s", str(e))
            return None

    async def _handle_bulk_action(
        self, body: dict, client: AsyncWebClient, status: ProposalStatus
    ) -> None:
        """Handle Approve All / Reject All button click."""
        import asyncio

        action = body["actions"][0]
        value = json.loads(action["value"])
        session_uuid = value["session_uuid"]
        user_id = body["user"]["id"]
        channel_id = body["channel"]["id"]
        summary_ts = body["message"]["ts"]

        # Update all PENDING proposals
        updated_proposals = await self.db.bulk_update_pending_proposals(
            session_uuid, status, user_id
        )

        if not updated_proposals:
            # All proposals were already individually handled
            await client.chat_update(
                channel=channel_id,
                ts=summary_ts,
                blocks=[{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "All proposals were already individually reviewed.",
                    },
                }],
                text="All proposals already reviewed",
            )
            return

        # Update individual Slack messages for each affected proposal
        status_emoji = (
            self.settings.approved_emoji
            if status == ProposalStatus.APPROVED
            else self.settings.rejected_emoji
        )
        status_text = "Bulk Approved" if status == ProposalStatus.APPROVED else "Bulk Rejected"

        for proposal in updated_proposals:
            if proposal.slack_message_ts:
                try:
                    result = await client.conversations_history(
                        channel=channel_id,
                        latest=proposal.slack_message_ts,
                        inclusive=True,
                        limit=1,
                    )
                    if result["messages"]:
                        original_blocks = result["messages"][0].get("blocks", [])
                        updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
                        updated_blocks.append({
                            "type": "context",
                            "elements": [{
                                "type": "mrkdwn",
                                "text": f":{status_emoji}: *{status_text}* by <@{user_id}>",
                            }],
                        })
                        await client.chat_update(
                            channel=channel_id,
                            ts=proposal.slack_message_ts,
                            blocks=updated_blocks,
                            text=f"Proposal {status_text}",
                        )
                except Exception as e:
                    logger.error(
                        "Failed to update proposal message %s: %s",
                        proposal.proposal_id, e,
                    )
                # Throttle to avoid Slack rate limits
                await asyncio.sleep(0.3)

        # Update summary message with final tally
        all_proposals = await self.db.get_proposals_for_session(session_uuid)
        approved = sum(1 for p in all_proposals if p.status == ProposalStatus.APPROVED)
        rejected = sum(1 for p in all_proposals if p.status == ProposalStatus.REJECTED)

        await client.chat_update(
            channel=channel_id,
            ts=summary_ts,
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Session Complete:* {approved} approved, {rejected} rejected by <@{user_id}>",
                },
            }],
            text=f"Session complete: {approved} approved, {rejected} rejected",
        )

        # Update session counts
        await self.db.update_session_counts(session_uuid, len(all_proposals), approved, rejected)

        # Audit log
        audit_event = (
            AuditEventType.BULK_APPROVED
            if status == ProposalStatus.APPROVED
            else AuditEventType.BULK_REJECTED
        )
        await self.db.append_audit_log(AuditEntry(
            event_type=audit_event,
            user_id=user_id,
            session_uuid=session_uuid,
            metadata={"affected_proposal_ids": [p.proposal_id for p in updated_proposals]},
        ))

        # Check if all responded and send to LLM
        all_responded = await self.db.are_all_proposals_responded(session_uuid)
        if all_responded:
            await self._send_approval_decisions_to_llm(session_uuid, channel_id, client)

    async def _open_rejection_reason_modal(self, body: dict, client: AsyncWebClient) -> None:
        """Open modal to collect optional rejection reason."""
        action = body["actions"][0]
        value = json.loads(action["value"])

        view = {
            "type": "modal",
            "callback_id": "rejection_reason_modal",
            "title": {"type": "plain_text", "text": "Reject Proposal"},
            "submit": {"type": "plain_text", "text": "Reject"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({
                "session_uuid": value["session_uuid"],
                "proposal_id": value["proposal_id"],
                "channel_id": body["channel"]["id"],
                "message_ts": body["message"]["ts"],
            }),
            "blocks": [
                {
                    "type": "input",
                    "block_id": "reason_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "reason_input",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "Why are you rejecting this? (optional)"},
                    },
                    "label": {"type": "plain_text", "text": "Rejection Reason"},
                    "optional": True,
                },
            ],
        }

        await client.views_open(trigger_id=body["trigger_id"], view=view)

    async def _handle_rejection_reason_submission(
        self, body: dict, client: AsyncWebClient, view: dict
    ) -> None:
        """Handle rejection reason modal submission — reject the proposal and record the pattern."""
        user_id = body["user"]["id"]
        metadata = json.loads(view["private_metadata"])
        session_uuid = metadata["session_uuid"]
        proposal_id = metadata["proposal_id"]
        channel_id = metadata["channel_id"]
        message_ts = metadata["message_ts"]

        # Extract optional reason
        reason_values = view["state"]["values"].get("reason_block", {})
        reason = reason_values.get("reason_input", {}).get("value")

        # Get proposal before rejection for pattern recording
        proposal = await self.db.get_proposal_by_id(session_uuid, proposal_id)
        if not proposal or proposal.status != ProposalStatus.PENDING:
            return

        # Update proposal status
        await self.db.update_proposal_status(
            session_uuid, proposal_id, ProposalStatus.REJECTED, user_id
        )

        # Record rejection pattern
        await self.db.record_rejection(
            session_uuid=session_uuid,
            proposal_id=proposal_id,
            change_type=proposal.change_type,
            rejected_by=user_id,
            field_name=proposal.field_name,
            confidence=proposal.confidence,
            source_type=proposal.source,
            rejection_reason=reason,
        )

        # Audit log
        await self.db.append_audit_log(AuditEntry(
            event_type=AuditEventType.PROPOSAL_REJECTED,
            user_id=user_id,
            session_uuid=session_uuid,
            proposal_id=proposal_id,
            before_snapshot={"status": "pending", "proposed_value": proposal.proposed_value},
            after_snapshot={"status": "rejected", "rejection_reason": reason},
        ))

        # Update Slack message (remove buttons, show "Rejected")
        try:
            result = await client.conversations_history(
                channel=channel_id, latest=message_ts, inclusive=True, limit=1,
            )
            if result["messages"]:
                original_blocks = result["messages"][0].get("blocks", [])
                updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
                reason_text = f"\n_Reason: {reason}_" if reason else ""
                updated_blocks.append({
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": f":{self.settings.rejected_emoji}: *Rejected* by <@{user_id}>{reason_text}",
                    }],
                })
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    blocks=updated_blocks,
                    text="Proposal Rejected",
                )
        except Exception as e:
            logger.error("Failed to update rejected proposal message: %s", e)

        # Check if all responded
        all_responded = await self.db.are_all_proposals_responded(session_uuid)
        if all_responded:
            await self._update_summary_message_complete(session_uuid, channel_id, user_id, client)
            await self._send_approval_decisions_to_llm(session_uuid, channel_id, client)

    async def _open_edit_modal(self, body: dict, client: AsyncWebClient) -> None:
        """Open modal to edit a proposal's proposed value before approving."""
        action = body["actions"][0]
        value = json.loads(action["value"])
        session_uuid = value["session_uuid"]
        proposal_id = value["proposal_id"]
        trigger_id = body["trigger_id"]

        proposal = await self.db.get_proposal_by_id(session_uuid, proposal_id)
        if not proposal or proposal.status != ProposalStatus.PENDING:
            return

        # Format the proposed value for editing
        if isinstance(proposal.proposed_value, dict):
            edit_text = json.dumps(proposal.proposed_value, indent=2)
        else:
            edit_text = str(proposal.proposed_value or "")

        # Slack plain_text_input initial_value limit is 3000 chars
        truncated = len(edit_text) > 3000
        if truncated:
            edit_text = edit_text[:3000]

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Ticket:* {proposal.ticket_key}\n*Change:* {proposal.change_type}\n*Field:* {proposal.field_name or 'N/A'}",
                },
            },
        ]

        # Show current value as read-only context
        if proposal.current_value:
            current_display = str(proposal.current_value)[:500]
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Current value:*\n```{current_display}```",
                },
            })

        blocks.append({"type": "divider"})

        if truncated:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "_Value was truncated to 3000 chars for editing._"}],
            })

        blocks.append({
            "type": "input",
            "block_id": "edited_value_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "edited_value_input",
                "multiline": True,
                "initial_value": edit_text,
            },
            "label": {"type": "plain_text", "text": "Proposed Value (edit below)"},
        })

        view = {
            "type": "modal",
            "callback_id": "edit_proposal_modal",
            "title": {"type": "plain_text", "text": "Edit Proposal"},
            "submit": {"type": "plain_text", "text": "Save & Approve"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({
                "session_uuid": session_uuid,
                "proposal_id": proposal_id,
                "channel_id": body["channel"]["id"],
                "message_ts": body["message"]["ts"],
            }),
            "blocks": blocks,
        }

        await client.views_open(trigger_id=trigger_id, view=view)

    async def _handle_edit_submission(
        self, body: dict, client: AsyncWebClient, view: dict
    ) -> None:
        """Handle edit proposal modal submission — save edited value and approve."""
        user_id = body["user"]["id"]
        metadata = json.loads(view["private_metadata"])
        session_uuid = metadata["session_uuid"]
        proposal_id = metadata["proposal_id"]
        channel_id = metadata["channel_id"]
        message_ts = metadata["message_ts"]

        new_value = view["state"]["values"]["edited_value_block"]["edited_value_input"]["value"]

        # Get old proposal for audit before_snapshot
        old_proposal = await self.db.get_proposal_by_id(session_uuid, proposal_id)
        if not old_proposal or old_proposal.status != ProposalStatus.PENDING:
            return

        # Update DB (sets status to APPROVED, stores original)
        await self.db.update_proposal_value(
            session_uuid, proposal_id, new_value, user_id
        )

        # Audit log
        await self.db.append_audit_log(AuditEntry(
            event_type=AuditEventType.PROPOSAL_EDITED,
            user_id=user_id,
            session_uuid=session_uuid,
            proposal_id=proposal_id,
            before_snapshot={"proposed_value": old_proposal.proposed_value},
            after_snapshot={"proposed_value": new_value},
        ))

        # Update Slack message (remove buttons, show "Edited & Approved")
        try:
            result = await client.conversations_history(
                channel=channel_id, latest=message_ts, inclusive=True, limit=1,
            )
            if result["messages"]:
                original_blocks = result["messages"][0].get("blocks", [])
                updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
                updated_blocks.append({
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": f":pencil: *Edited & Approved* by <@{user_id}>",
                    }],
                })
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    blocks=updated_blocks,
                    text="Proposal Edited & Approved",
                )
        except Exception as e:
            logger.error("Failed to update edited proposal message: %s", e)

        # Check if all responded
        all_responded = await self.db.are_all_proposals_responded(session_uuid)
        if all_responded:
            await self._update_summary_message_complete(session_uuid, channel_id, user_id, client)
            await self._send_approval_decisions_to_llm(session_uuid, channel_id, client)

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

        # Get proposal before update for audit snapshot
        proposal_before = await self.db.get_proposal_by_id(session_uuid, proposal_id)

        # Update proposal status in DB
        await self.db.update_proposal_status(
            session_uuid, proposal_id, status, user_id
        )

        # Audit: proposal approved/rejected
        audit_event = (
            AuditEventType.PROPOSAL_APPROVED
            if status == ProposalStatus.APPROVED
            else AuditEventType.PROPOSAL_REJECTED
        )
        await self.db.append_audit_log(AuditEntry(
            event_type=audit_event,
            user_id=user_id,
            session_uuid=session_uuid,
            proposal_id=proposal_id,
            before_snapshot={
                "status": proposal_before.status.value if proposal_before else "unknown",
                "proposed_value": proposal_before.proposed_value if proposal_before else None,
            },
            after_snapshot={"status": status.value},
        ))

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
            # Update summary message to remove bulk buttons and show final tally
            await self._update_summary_message_complete(session_uuid, channel_id, user_id, client)
            await self._send_approval_decisions_to_llm(session_uuid, channel_id, client)

    async def _update_summary_message_complete(
        self,
        session_uuid: str,
        channel_id: str,
        user_id: str,
        client: AsyncWebClient,
    ) -> None:
        """Update the bulk summary message to show final tally and remove buttons."""
        summary_ts = await self.db.get_session_summary_ts(session_uuid)
        if not summary_ts:
            return

        all_proposals = await self.db.get_proposals_for_session(session_uuid)
        approved = sum(1 for p in all_proposals if p.status == ProposalStatus.APPROVED)
        rejected = sum(1 for p in all_proposals if p.status == ProposalStatus.REJECTED)

        try:
            await client.chat_update(
                channel=channel_id,
                ts=summary_ts,
                blocks=[{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Session Complete:* {approved} approved, {rejected} rejected",
                    },
                }],
                text=f"Session complete: {approved} approved, {rejected} rejected",
            )
        except Exception as e:
            logger.error("Failed to update summary message: %s", e)

        await self.db.update_session_counts(session_uuid, len(all_proposals), approved, rejected)

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
                        shared_jira=self._get_shared_jira_config(),
                    )
                else:
                    # No PM config — still pass shared JIRA creds for execution
                    shared_jira = self._get_shared_jira_config()
                    if shared_jira:
                        extra_tweaks = {
                            COMPONENT_ID_JIRA_READER_WRITER: {**shared_jira, "auth_type": "basic"},
                            COMPONENT_ID_JIRA_STATE_FETCHER: {**shared_jira, "auth_type": "basic"},
                        }

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

        # Audit: decisions sent to LLM
        await self.db.append_audit_log(AuditEntry(
            event_type=AuditEventType.DECISIONS_SENT_TO_LLM,
            session_uuid=session_uuid,
            metadata={
                "approved_count": approved_count,
                "rejected_count": rejected_count,
                "decisions": decisions,
            },
        ))

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

    def _get_shared_jira_config(self) -> Optional[dict[str, str]]:
        """Build shared JIRA service account config from env settings.

        Returns None if shared JIRA is not configured.
        """
        if not self.settings.jira_shared_api_token:
            return None
        return {
            "jira_url": self.settings.jira_shared_url or "",
            "email": self.settings.jira_shared_email or "",
            "api_token": self.settings.jira_shared_api_token,
        }

    async def _manual_check_transcripts(
        self, client: AsyncWebClient, channel_id: str, user_id: str
    ) -> None:
        """Manually trigger transcript check for the requesting PM."""
        if not self._scheduler:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text="Transcript scheduler is not available.",
            )
            return

        pm_config = await self.dynamodb.get_pm_config(user_id)
        if not pm_config:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text="No configuration found. Run `/jira-agent setup` first.",
            )
            return

        await client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="Checking for new transcripts...",
        )

        try:
            default_gdrive = self._get_default_gdrive_config()
            found = await self._scheduler._check_pm(pm_config, default_gdrive)
            if not found:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text="No new transcripts found. Latest transcript has already been processed.",
                )
        except Exception as e:
            logger.exception("Manual transcript check failed for %s", user_id)
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"Transcript check failed: {e}",
            )

    # ==========================================
    # PM ONBOARDING MODALS
    # ==========================================

    async def _open_setup_modal(
        self, client: AsyncWebClient, trigger_id: str, user_id: str
    ) -> None:
        """Open the full PM setup modal."""
        # Check if user already has a config
        existing = await self.dynamodb.get_pm_config(user_id)

        shared_jira_configured = bool(self.settings.jira_shared_api_token)

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
        ]

        if shared_jira_configured:
            # Shared service token — PM only needs project keys
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "_Using shared JIRA service account. You only need to provide your project key(s)._"}],
            })
        else:
            # Individual JIRA credentials
            blocks.extend([
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
            ])

        blocks.extend([
            {
                "type": "input", "block_id": "jira_project_block",
                "element": {
                    "type": "plain_text_input", "action_id": "jira_project_input",
                    "placeholder": {"type": "plain_text", "text": "LAN, PROJ2, INFRA"},
                    **({"initial_value": ",".join(existing["jira_config"].get("project_keys", [existing["jira_config"].get("project_key", "")]))} if existing and existing.get("jira_config", {}).get("project_key") else {}),
                },
                "label": {"type": "plain_text", "text": "JIRA Project Keys (comma-separated)"},
            },
            {"type": "divider"},
            {"type": "header", "text": {"type": "plain_text", "text": "Google Drive"}},
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
        ])

        # Flag whether this is an update (secrets will be read from DynamoDB on submission)
        if existing:
            private_metadata = json.dumps({"is_update": True})
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
                    "placeholder": {"type": "plain_text", "text": "LAN, PROJ2, INFRA"},
                    **({"initial_value": ",".join(jira.get("project_keys", [jira.get("project_key", "")]))} if jira.get("project_key") or jira.get("project_keys") else {}),
                },
                "label": {"type": "plain_text", "text": "JIRA Project Keys (comma-separated)"},
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

        text = (
            f"*Your JIRA Agent Configuration*\n\n"
            f"*Name:* {config.get('name', 'N/A')}\n"
            f"*Email:* {config.get('email', 'N/A')}\n"
            f"*Enabled:* {config.get('enabled', False)}\n\n"
            f"*JIRA:*\n"
            f"  URL: `{jira.get('jira_url', 'N/A')}`\n"
            f"  Email: `{jira.get('email', 'N/A')}`\n"
            f"  API Token: `{token_masked}`\n"
            f"  Projects: `{', '.join(jira.get('project_keys', [jira.get('project_key', 'N/A')]))}`\n\n"
            f"*Google Drive:*\n"
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

    async def _open_schedule_modal(
        self, client: AsyncWebClient, trigger_id: str, user_id: str
    ) -> None:
        """Open schedule configuration modal."""
        existing = await self.dynamodb.get_pm_config(user_id) if self.dynamodb else None
        schedule = existing.get("schedule_config", {}) if existing else {}

        blocks = [
            {
                "type": "input", "block_id": "schedule_enabled_block",
                "element": {
                    "type": "checkboxes", "action_id": "schedule_enabled_input",
                    "options": [{
                        "text": {"type": "plain_text", "text": "Enable recurring sync"},
                        "value": "enabled",
                    }],
                    **({"initial_options": [{
                        "text": {"type": "plain_text", "text": "Enable recurring sync"},
                        "value": "enabled",
                    }]} if schedule.get("enabled") else {}),
                },
                "label": {"type": "plain_text", "text": "Schedule"},
                "optional": True,
            },
            {
                "type": "input", "block_id": "cron_preset_block",
                "element": {
                    "type": "static_select", "action_id": "cron_preset_input",
                    "options": [
                        {"text": {"type": "plain_text", "text": "Daily at 9 AM"}, "value": "0 9 * * *"},
                        {"text": {"type": "plain_text", "text": "Weekdays at 9 AM"}, "value": "0 9 * * 1-5"},
                        {"text": {"type": "plain_text", "text": "Every Monday at 9 AM"}, "value": "0 9 * * 1"},
                        {"text": {"type": "plain_text", "text": "Custom (enter below)"}, "value": "custom"},
                    ],
                },
                "label": {"type": "plain_text", "text": "Frequency"},
            },
            {
                "type": "input", "block_id": "cron_custom_block",
                "element": {
                    "type": "plain_text_input", "action_id": "cron_custom_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. every weekday at 3pm"},
                    **({"initial_value": schedule.get("cron_expression", "")} if schedule.get("cron_expression") else {}),
                },
                "label": {"type": "plain_text", "text": "Describe your schedule (only if Custom selected above)"},
                "optional": True,
            },
            {
                "type": "input", "block_id": "timezone_block",
                "element": {
                    "type": "static_select", "action_id": "timezone_input",
                    "options": [
                        {"text": {"type": "plain_text", "text": "US/Eastern"}, "value": "America/New_York"},
                        {"text": {"type": "plain_text", "text": "US/Central"}, "value": "America/Chicago"},
                        {"text": {"type": "plain_text", "text": "US/Pacific"}, "value": "America/Los_Angeles"},
                        {"text": {"type": "plain_text", "text": "UTC"}, "value": "UTC"},
                        {"text": {"type": "plain_text", "text": "Europe/London"}, "value": "Europe/London"},
                        {"text": {"type": "plain_text", "text": "Europe/Berlin"}, "value": "Europe/Berlin"},
                        {"text": {"type": "plain_text", "text": "Asia/Tokyo"}, "value": "Asia/Tokyo"},
                    ],
                },
                "label": {"type": "plain_text", "text": "Timezone"},
            },
            {
                "type": "input", "block_id": "target_channel_block",
                "element": {
                    "type": "conversations_select", "action_id": "target_channel_input",
                    **({"default_to_current_conversation": True}),
                },
                "label": {"type": "plain_text", "text": "Results Channel"},
            },
        ]

        view = {
            "type": "modal",
            "callback_id": "schedule_config_modal",
            "title": {"type": "plain_text", "text": "Sync Schedule"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": blocks,
        }

        await client.views_open(trigger_id=trigger_id, view=view)

    async def _handle_schedule_submission(
        self, body: dict, client: AsyncWebClient, view: dict
    ) -> None:
        """Handle schedule config modal submission."""
        user_id = body["user"]["id"]
        values = view["state"]["values"]

        enabled_options = values["schedule_enabled_block"]["schedule_enabled_input"].get("selected_options", [])
        enabled = any(o.get("value") == "enabled" for o in enabled_options)

        cron_preset = values["cron_preset_block"]["cron_preset_input"].get("selected_option", {}).get("value", "")
        cron_custom = (values["cron_custom_block"]["cron_custom_input"].get("value") or "").strip()

        schedule_description = ""
        if cron_preset == "custom" and cron_custom:
            if self.settings.anthropic_api_key:
                cron_expression = await natural_language_to_cron(cron_custom, self.settings.anthropic_api_key)
                schedule_description = cron_custom
            else:
                cron_expression = cron_custom
        else:
            cron_expression = cron_preset

        from croniter import croniter
        if not croniter.is_valid(cron_expression):
            await client.chat_postMessage(
                channel=user_id,
                text=f"Invalid schedule: `{cron_expression}`. Please use a valid cron expression or natural language description.",
            )
            return

        tz = values["timezone_block"]["timezone_input"].get("selected_option", {}).get("value", "UTC")
        target_channel = values["target_channel_block"]["target_channel_input"].get("selected_conversation", user_id)

        schedule_config = {
            "enabled": enabled,
            "cron_expression": cron_expression,
            "timezone": tz,
            "target_channel": target_channel,
            "last_scheduled_run": "",
        }
        if schedule_description:
            schedule_config["schedule_description"] = schedule_description

        try:
            await self.dynamodb.update_pm(user_id, {"schedule_config": schedule_config})
            status = "enabled" if enabled else "disabled"
            confirm_msg = f"Sync schedule {status}. Cron: `{cron_expression}` ({tz}), Channel: <#{target_channel}>"
            if schedule_description:
                confirm_msg += f"\nInterpreted as: `{cron_expression}` (from \"{schedule_description}\")"
            await client.chat_postMessage(
                channel=user_id,
                text=confirm_msg,
            )
        except Exception as e:
            logger.exception("Failed to save schedule config")
            await client.chat_postMessage(
                channel=user_id,
                text=f"Failed to save schedule: {str(e)}",
            )

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

    async def _admin_audit_log(
        self, client: AsyncWebClient, channel_id: str, user_id: str,
        session_uuid: Optional[str] = None,
    ) -> None:
        """Show audit log entries for a session or recent entries."""
        try:
            entries = await self.db.get_audit_log(
                session_uuid=session_uuid, limit=25
            )
        except Exception as e:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"Failed to fetch audit log: {str(e)}",
            )
            return

        if not entries:
            await client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text="No audit log entries found.",
            )
            return

        header = f"*Audit Log* (session: `{session_uuid}`)" if session_uuid else "*Audit Log* (latest 25)"
        lines = [header, ""]
        for entry in entries:
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            user_display = f"<@{entry.user_id}>" if entry.user_id else "system"
            line = f"`{ts}` | `{entry.event_type.value}` | {user_display}"
            if entry.proposal_id:
                line += f" | proposal: `{entry.proposal_id}`"
            lines.append(line)

        await client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="\n".join(lines)
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
