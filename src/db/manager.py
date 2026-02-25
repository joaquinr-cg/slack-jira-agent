"""Database manager for JIRA Slack Agent."""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional
import logging

import aiosqlite

from .models import (
    AuditEntry,
    AuditEventType,
    Session,
    SessionStatus,
    MarkedMessage,
    MarkType,
    Proposal,
    ProposalStatus,
)

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages all database operations for the JIRA Slack Agent."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._initialized = False

    @staticmethod
    def _serialize_value(value) -> Optional[str]:
        """Serialize a value to string for SQLite storage.

        If value is a dict or list, serialize to JSON string.
        Otherwise return as-is (or None).
        """
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    async def initialize(self) -> None:
        """Initialize database schema."""
        if self._initialized:
            return

        async with aiosqlite.connect(self.db_path) as db:
            # Sessions table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    uuid TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    triggered_by TEXT NOT NULL,
                    triggered_at TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'pending',
                    completed_at TIMESTAMP,
                    error_message TEXT,
                    total_proposals INTEGER DEFAULT 0,
                    approved_count INTEGER DEFAULT 0,
                    rejected_count INTEGER DEFAULT 0
                )
            """)

            # Marked messages table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS marked_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    thread_ts TEXT,
                    message_text TEXT,
                    marked_by TEXT NOT NULL,
                    marked_at TIMESTAMP NOT NULL,
                    mark_type TEXT NOT NULL,
                    processed_in_session TEXT,
                    UNIQUE(channel_id, message_ts)
                )
            """)

            # Proposals table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_uuid TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    ticket_key TEXT NOT NULL,
                    ticket_summary TEXT,
                    change_type TEXT NOT NULL,
                    field_name TEXT,
                    current_value TEXT,
                    proposed_value TEXT,
                    source TEXT,
                    source_excerpt TEXT,
                    confidence TEXT DEFAULT 'medium',
                    status TEXT DEFAULT 'pending',
                    reviewed_by TEXT,
                    reviewed_at TIMESTAMP,
                    executed_at TIMESTAMP,
                    execution_error TEXT,
                    slack_message_ts TEXT,
                    UNIQUE(session_uuid, proposal_id),
                    FOREIGN KEY (session_uuid) REFERENCES sessions(uuid)
                )
            """)

            # Migrations: add new columns to existing tables
            for migration in [
                "ALTER TABLE sessions ADD COLUMN summary_message_ts TEXT",
                "ALTER TABLE proposals ADD COLUMN original_proposed_value TEXT",
                "ALTER TABLE proposals ADD COLUMN edited_by TEXT",
            ]:
                try:
                    await db.execute(migration)
                except Exception:
                    pass  # Column already exists

            # Rejection patterns table (for learning from rejections)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS rejection_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_uuid TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    field_name TEXT,
                    confidence TEXT,
                    source_type TEXT,
                    rejection_reason TEXT,
                    rejected_by TEXT NOT NULL,
                    rejected_at TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_rejection_patterns_type
                ON rejection_patterns(change_type)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_rejection_patterns_field
                ON rejection_patterns(field_name)
            """)

            # Audit log table (immutable: INSERT only)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    user_id TEXT,
                    session_uuid TEXT,
                    proposal_id TEXT,
                    before_snapshot TEXT,
                    after_snapshot TEXT,
                    metadata TEXT
                )
            """)

            # Create indexes
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_audit_log_session
                ON audit_log(session_uuid)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_audit_log_event
                ON audit_log(event_type)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
                ON audit_log(timestamp)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_marked_messages_channel
                ON marked_messages(channel_id)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_marked_messages_processed
                ON marked_messages(processed_in_session)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_proposals_session
                ON proposals(session_uuid)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_proposals_status
                ON proposals(status)
            """)

            await db.commit()

        self._initialized = True
        logger.info("Database initialized at %s", self.db_path)

    # ==========================================
    # SESSION OPERATIONS
    # ==========================================

    async def create_session(self, channel_id: str, triggered_by: str) -> Session:
        """Create a new sync session."""
        session = Session(
            uuid=str(uuid.uuid4()),
            channel_id=channel_id,
            triggered_by=triggered_by,
            triggered_at=datetime.now(timezone.utc),
            status=SessionStatus.PENDING,
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO sessions (uuid, channel_id, triggered_by, triggered_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session.uuid,
                    session.channel_id,
                    session.triggered_by,
                    session.triggered_at.isoformat(),
                    session.status.value,
                ),
            )
            await db.commit()

        logger.info("Created session %s for channel %s", session.uuid, channel_id)
        return session

    async def get_session(self, session_uuid: str) -> Optional[Session]:
        """Get a session by UUID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sessions WHERE uuid = ?", (session_uuid,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return self._row_to_session(row)
        return None

    async def update_session_status(
        self,
        session_uuid: str,
        status: SessionStatus,
        error_message: Optional[str] = None,
    ) -> None:
        """Update session status."""
        async with aiosqlite.connect(self.db_path) as db:
            if status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
                await db.execute(
                    """
                    UPDATE sessions
                    SET status = ?, completed_at = ?, error_message = ?
                    WHERE uuid = ?
                    """,
                    (status.value, datetime.now(timezone.utc).isoformat(), error_message, session_uuid),
                )
            else:
                await db.execute(
                    "UPDATE sessions SET status = ? WHERE uuid = ?",
                    (status.value, session_uuid),
                )
            await db.commit()

    async def update_session_counts(
        self, session_uuid: str, total: int, approved: int, rejected: int
    ) -> None:
        """Update proposal counts for a session."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE sessions
                SET total_proposals = ?, approved_count = ?, rejected_count = ?
                WHERE uuid = ?
                """,
                (total, approved, rejected, session_uuid),
            )
            await db.commit()

    def _row_to_session(self, row: aiosqlite.Row) -> Session:
        """Convert database row to Session object."""
        return Session(
            uuid=row["uuid"],
            channel_id=row["channel_id"],
            triggered_by=row["triggered_by"],
            triggered_at=datetime.fromisoformat(row["triggered_at"]),
            status=SessionStatus(row["status"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            error_message=row["error_message"],
            total_proposals=row["total_proposals"] or 0,
            approved_count=row["approved_count"] or 0,
            rejected_count=row["rejected_count"] or 0,
            summary_message_ts=row["summary_message_ts"] if "summary_message_ts" in row.keys() else None,
        )

    async def update_session_summary_ts(self, session_uuid: str, ts: str) -> None:
        """Store the Slack message_ts of the bulk summary message."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET summary_message_ts = ? WHERE uuid = ?",
                (ts, session_uuid),
            )
            await db.commit()

    async def get_session_summary_ts(self, session_uuid: str) -> Optional[str]:
        """Get the summary message ts for a session."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT summary_message_ts FROM sessions WHERE uuid = ?",
                (session_uuid,),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def bulk_update_pending_proposals(
        self,
        session_uuid: str,
        status: ProposalStatus,
        reviewed_by: str,
    ) -> list[Proposal]:
        """Update all PENDING proposals in a session to the given status.

        Returns the list of proposals that were actually updated.
        """
        now = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # First, fetch all pending proposals
            async with db.execute(
                "SELECT * FROM proposals WHERE session_uuid = ? AND status = ?",
                (session_uuid, ProposalStatus.PENDING.value),
            ) as cursor:
                rows = await cursor.fetchall()
                proposals = [self._row_to_proposal(row) for row in rows]

            if not proposals:
                return []

            # Update them all in one transaction
            await db.execute(
                """
                UPDATE proposals
                SET status = ?, reviewed_by = ?, reviewed_at = ?
                WHERE session_uuid = ? AND status = ?
                """,
                (status.value, reviewed_by, now, session_uuid, ProposalStatus.PENDING.value),
            )
            await db.commit()

        logger.info(
            "Bulk updated %d proposals to %s in session %s",
            len(proposals), status.value, session_uuid,
        )
        return proposals

    # ==========================================
    # MARKED MESSAGE OPERATIONS
    # ==========================================

    async def mark_message(
        self,
        channel_id: str,
        message_ts: str,
        marked_by: str,
        mark_type: MarkType,
        thread_ts: Optional[str] = None,
        message_text: Optional[str] = None,
    ) -> MarkedMessage:
        """Mark a message for JIRA review."""
        message = MarkedMessage(
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=thread_ts,
            message_text=message_text,
            marked_by=marked_by,
            marked_at=datetime.now(timezone.utc),
            mark_type=mark_type,
        )

        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    """
                    INSERT INTO marked_messages
                    (channel_id, message_ts, thread_ts, message_text, marked_by, marked_at, mark_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.channel_id,
                        message.message_ts,
                        message.thread_ts,
                        message.message_text,
                        message.marked_by,
                        message.marked_at.isoformat(),
                        message.mark_type.value,
                    ),
                )
                await db.commit()
                logger.info("Marked message %s in channel %s", message_ts, channel_id)
            except aiosqlite.IntegrityError:
                logger.debug("Message %s already marked", message_ts)

        return message

    async def unmark_message(self, channel_id: str, message_ts: str) -> bool:
        """Remove mark from a message."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                DELETE FROM marked_messages
                WHERE channel_id = ? AND message_ts = ? AND processed_in_session IS NULL
                """,
                (channel_id, message_ts),
            )
            await db.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            logger.info("Unmarked message %s in channel %s", message_ts, channel_id)
        return deleted

    async def get_unprocessed_marked_messages(
        self, channel_id: Optional[str] = None
    ) -> list[MarkedMessage]:
        """Get all marked messages that haven't been processed yet."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if channel_id:
                query = """
                    SELECT * FROM marked_messages
                    WHERE processed_in_session IS NULL AND channel_id = ?
                    ORDER BY marked_at ASC
                """
                params = (channel_id,)
            else:
                query = """
                    SELECT * FROM marked_messages
                    WHERE processed_in_session IS NULL
                    ORDER BY marked_at ASC
                """
                params = ()

            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_marked_message(row) for row in rows]

    async def mark_messages_as_processed(
        self, message_ids: list[int], session_uuid: str
    ) -> None:
        """Mark messages as processed by a session."""
        if not message_ids:
            return

        async with aiosqlite.connect(self.db_path) as db:
            placeholders = ",".join("?" * len(message_ids))
            await db.execute(
                f"""
                UPDATE marked_messages
                SET processed_in_session = ?
                WHERE id IN ({placeholders})
                """,
                (session_uuid, *message_ids),
            )
            await db.commit()

    def _row_to_marked_message(self, row: aiosqlite.Row) -> MarkedMessage:
        """Convert database row to MarkedMessage object."""
        return MarkedMessage(
            id=row["id"],
            channel_id=row["channel_id"],
            message_ts=row["message_ts"],
            thread_ts=row["thread_ts"],
            message_text=row["message_text"],
            marked_by=row["marked_by"],
            marked_at=datetime.fromisoformat(row["marked_at"]),
            mark_type=MarkType(row["mark_type"]),
            processed_in_session=row["processed_in_session"],
        )

    # ==========================================
    # PROPOSAL OPERATIONS
    # ==========================================

    async def create_proposal(self, proposal: Proposal) -> Proposal:
        """Create a new proposal."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO proposals
                (session_uuid, proposal_id, ticket_key, ticket_summary, change_type,
                 field_name, current_value, proposed_value, source, source_excerpt,
                 confidence, status, slack_message_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.session_uuid,
                    proposal.proposal_id,
                    proposal.ticket_key,
                    proposal.ticket_summary,
                    proposal.change_type,
                    proposal.field_name,
                    self._serialize_value(proposal.current_value),
                    self._serialize_value(proposal.proposed_value),
                    proposal.source,
                    proposal.source_excerpt,
                    proposal.confidence,
                    proposal.status.value,
                    proposal.slack_message_ts,
                ),
            )
            proposal.id = cursor.lastrowid
            await db.commit()

        logger.info(
            "Created proposal %s for ticket %s in session %s",
            proposal.proposal_id,
            proposal.ticket_key,
            proposal.session_uuid,
        )
        return proposal

    async def create_proposals_batch(self, proposals: list[Proposal]) -> list[Proposal]:
        """Create multiple proposals in a batch."""
        async with aiosqlite.connect(self.db_path) as db:
            for proposal in proposals:
                cursor = await db.execute(
                    """
                    INSERT INTO proposals
                    (session_uuid, proposal_id, ticket_key, ticket_summary, change_type,
                     field_name, current_value, proposed_value, source, source_excerpt,
                     confidence, status, slack_message_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.session_uuid,
                        proposal.proposal_id,
                        proposal.ticket_key,
                        proposal.ticket_summary,
                        proposal.change_type,
                        proposal.field_name,
                        self._serialize_value(proposal.current_value),
                        self._serialize_value(proposal.proposed_value),
                        proposal.source,
                        proposal.source_excerpt,
                        proposal.confidence,
                        proposal.status.value,
                        proposal.slack_message_ts,
                    ),
                )
                proposal.id = cursor.lastrowid
            await db.commit()

        logger.info("Created %d proposals in batch", len(proposals))
        return proposals

    async def get_proposals_for_session(self, session_uuid: str) -> list[Proposal]:
        """Get all proposals for a session."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM proposals WHERE session_uuid = ? ORDER BY id",
                (session_uuid,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_proposal(row) for row in rows]

    async def get_proposal_by_id(
        self, session_uuid: str, proposal_id: str
    ) -> Optional[Proposal]:
        """Get a specific proposal."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM proposals WHERE session_uuid = ? AND proposal_id = ?",
                (session_uuid, proposal_id),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return self._row_to_proposal(row)
        return None

    async def update_proposal_status(
        self,
        session_uuid: str,
        proposal_id: str,
        status: ProposalStatus,
        reviewed_by: Optional[str] = None,
    ) -> None:
        """Update proposal approval status."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE proposals
                SET status = ?, reviewed_by = ?, reviewed_at = ?
                WHERE session_uuid = ? AND proposal_id = ?
                """,
                (
                    status.value,
                    reviewed_by,
                    datetime.now(timezone.utc).isoformat() if reviewed_by else None,
                    session_uuid,
                    proposal_id,
                ),
            )
            await db.commit()

        logger.info(
            "Updated proposal %s status to %s (reviewed by %s)",
            proposal_id,
            status.value,
            reviewed_by,
        )

    async def update_proposal_value(
        self,
        session_uuid: str,
        proposal_id: str,
        new_value: str,
        edited_by: str,
    ) -> Optional[Proposal]:
        """Update proposed_value (storing original), then mark as APPROVED.

        Returns the updated proposal.
        """
        now = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Get current proposal to preserve original value
            async with db.execute(
                "SELECT * FROM proposals WHERE session_uuid = ? AND proposal_id = ?",
                (session_uuid, proposal_id),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                current = self._row_to_proposal(row)

            # Store original only if not already stored (first edit)
            original = current.original_proposed_value or current.proposed_value

            await db.execute(
                """
                UPDATE proposals
                SET proposed_value = ?, original_proposed_value = ?,
                    edited_by = ?, status = ?, reviewed_by = ?, reviewed_at = ?
                WHERE session_uuid = ? AND proposal_id = ?
                """,
                (
                    new_value, self._serialize_value(original),
                    edited_by, ProposalStatus.APPROVED.value, edited_by, now,
                    session_uuid, proposal_id,
                ),
            )
            await db.commit()

        logger.info("Proposal %s edited and approved by %s", proposal_id, edited_by)
        return await self.get_proposal_by_id(session_uuid, proposal_id)

    async def update_proposal_slack_ts(
        self, session_uuid: str, proposal_id: str, slack_message_ts: str
    ) -> None:
        """Update the Slack message timestamp for a proposal."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE proposals
                SET slack_message_ts = ?
                WHERE session_uuid = ? AND proposal_id = ?
                """,
                (slack_message_ts, session_uuid, proposal_id),
            )
            await db.commit()

    async def mark_proposal_executed(
        self,
        session_uuid: str,
        proposal_id: str,
        error: Optional[str] = None,
    ) -> None:
        """Mark a proposal as executed or failed."""
        status = ProposalStatus.FAILED if error else ProposalStatus.EXECUTED
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE proposals
                SET status = ?, executed_at = ?, execution_error = ?
                WHERE session_uuid = ? AND proposal_id = ?
                """,
                (
                    status.value,
                    datetime.now(timezone.utc).isoformat(),
                    error,
                    session_uuid,
                    proposal_id,
                ),
            )
            await db.commit()

    async def get_pending_proposals_count(self, session_uuid: str) -> int:
        """Get count of pending proposals for a session."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM proposals WHERE session_uuid = ? AND status = ?",
                (session_uuid, ProposalStatus.PENDING.value),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def are_all_proposals_responded(self, session_uuid: str) -> bool:
        """Check if all proposals in a session have been responded to."""
        pending_count = await self.get_pending_proposals_count(session_uuid)
        return pending_count == 0

    async def get_approved_proposals(self, session_uuid: str) -> list[Proposal]:
        """Get all approved proposals for a session."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM proposals WHERE session_uuid = ? AND status = ?",
                (session_uuid, ProposalStatus.APPROVED.value),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_proposal(row) for row in rows]

    def _row_to_proposal(self, row: aiosqlite.Row) -> Proposal:
        """Convert database row to Proposal object."""
        keys = row.keys()
        return Proposal(
            id=row["id"],
            session_uuid=row["session_uuid"],
            proposal_id=row["proposal_id"],
            ticket_key=row["ticket_key"],
            ticket_summary=row["ticket_summary"],
            change_type=row["change_type"],
            field_name=row["field_name"],
            current_value=row["current_value"],
            proposed_value=row["proposed_value"],
            source=row["source"],
            source_excerpt=row["source_excerpt"],
            confidence=row["confidence"],
            original_proposed_value=row["original_proposed_value"] if "original_proposed_value" in keys else None,
            edited_by=row["edited_by"] if "edited_by" in keys else None,
            status=ProposalStatus(row["status"]),
            reviewed_by=row["reviewed_by"],
            reviewed_at=datetime.fromisoformat(row["reviewed_at"]) if row["reviewed_at"] else None,
            executed_at=datetime.fromisoformat(row["executed_at"]) if row["executed_at"] else None,
            execution_error=row["execution_error"],
            slack_message_ts=row["slack_message_ts"],
        )

    # ==========================================
    # REJECTION PATTERN OPERATIONS
    # ==========================================

    async def record_rejection(
        self,
        session_uuid: str,
        proposal_id: str,
        change_type: str,
        rejected_by: str,
        field_name: Optional[str] = None,
        confidence: Optional[str] = None,
        source_type: Optional[str] = None,
        rejection_reason: Optional[str] = None,
    ) -> None:
        """Record a rejection pattern entry."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO rejection_patterns
                (session_uuid, proposal_id, change_type, field_name, confidence,
                 source_type, rejection_reason, rejected_by, rejected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_uuid, proposal_id, change_type, field_name,
                    confidence, source_type, rejection_reason, rejected_by,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()

    async def get_rejection_summary(self, min_count: int = 3) -> list[dict]:
        """Aggregate rejection patterns for feeding back to LLM.

        Returns patterns that have occurred at least min_count times.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    change_type,
                    field_name,
                    confidence,
                    COUNT(*) as total_rejections,
                    GROUP_CONCAT(DISTINCT rejection_reason) as sample_reasons
                FROM rejection_patterns
                GROUP BY change_type, field_name, confidence
                HAVING COUNT(*) >= ?
                ORDER BY total_rejections DESC
                LIMIT 20
                """,
                (min_count,),
            ) as cursor:
                rows = await cursor.fetchall()
                results = []
                for row in rows:
                    reasons = row["sample_reasons"]
                    results.append({
                        "change_type": row["change_type"],
                        "field_name": row["field_name"],
                        "confidence": row["confidence"],
                        "total_rejections": row["total_rejections"],
                        "sample_reasons": [r for r in (reasons or "").split(",") if r][:3],
                    })
                return results

    # ==========================================
    # AUDIT LOG OPERATIONS (immutable: INSERT + SELECT only)
    # ==========================================

    async def append_audit_log(self, entry: AuditEntry) -> None:
        """Append an entry to the audit log. INSERT-only, never updates or deletes."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO audit_log
                    (timestamp, event_type, user_id, session_uuid, proposal_id,
                     before_snapshot, after_snapshot, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.timestamp.isoformat(),
                        entry.event_type.value,
                        entry.user_id,
                        entry.session_uuid,
                        entry.proposal_id,
                        json.dumps(entry.before_snapshot) if entry.before_snapshot else None,
                        json.dumps(entry.after_snapshot) if entry.after_snapshot else None,
                        json.dumps(entry.metadata) if entry.metadata else None,
                    ),
                )
                await db.commit()
        except Exception as e:
            logger.error("Failed to write audit log: %s", e)

    async def get_audit_log(
        self,
        session_uuid: Optional[str] = None,
        event_type: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEntry]:
        """Query audit log with optional filters."""
        conditions = []
        params: list = []

        if session_uuid:
            conditions.append("session_uuid = ?")
            params.append(session_uuid)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = f"SELECT * FROM audit_log{where_clause} ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_audit_entry(row) for row in rows]

    def _row_to_audit_entry(self, row: aiosqlite.Row) -> AuditEntry:
        """Convert database row to AuditEntry object."""
        return AuditEntry(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            event_type=AuditEventType(row["event_type"]),
            user_id=row["user_id"],
            session_uuid=row["session_uuid"],
            proposal_id=row["proposal_id"],
            before_snapshot=json.loads(row["before_snapshot"]) if row["before_snapshot"] else None,
            after_snapshot=json.loads(row["after_snapshot"]) if row["after_snapshot"] else None,
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
        )

    # ==========================================
    # STATISTICS
    # ==========================================

    async def get_stats(self) -> dict:
        """Get database statistics."""
        async with aiosqlite.connect(self.db_path) as db:
            stats = {}

            async with db.execute("SELECT COUNT(*) FROM sessions") as cursor:
                stats["total_sessions"] = (await cursor.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM sessions WHERE status = ?",
                (SessionStatus.COMPLETED.value,),
            ) as cursor:
                stats["completed_sessions"] = (await cursor.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM marked_messages WHERE processed_in_session IS NULL"
            ) as cursor:
                stats["pending_marked_messages"] = (await cursor.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM proposals") as cursor:
                stats["total_proposals"] = (await cursor.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM proposals WHERE status = ?",
                (ProposalStatus.EXECUTED.value,),
            ) as cursor:
                stats["executed_proposals"] = (await cursor.fetchone())[0]

            return stats
