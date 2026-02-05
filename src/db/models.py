"""Database models and dataclasses for JIRA Slack Agent."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class SessionStatus(str, Enum):
    """Status of a sync session."""
    PENDING = "pending"
    PROCESSING = "processing"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class MarkType(str, Enum):
    """How a message was marked for review."""
    EMOJI = "emoji"
    COMMAND = "command"


class ProposalStatus(str, Enum):
    """Status of a single proposal."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass
class Session:
    """Represents a /jira-sync session."""
    uuid: str
    channel_id: str
    triggered_by: str
    triggered_at: datetime
    status: SessionStatus = SessionStatus.PENDING
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    total_proposals: int = 0
    approved_count: int = 0
    rejected_count: int = 0


@dataclass
class MarkedMessage:
    """A message marked for JIRA review."""
    id: Optional[int] = None
    channel_id: str = ""
    message_ts: str = ""
    thread_ts: Optional[str] = None
    message_text: Optional[str] = None
    marked_by: str = ""
    marked_at: datetime = field(default_factory=datetime.utcnow)
    mark_type: MarkType = MarkType.EMOJI
    processed_in_session: Optional[str] = None  # UUID of session

    @property
    def message_key(self) -> str:
        """Unique key for this message."""
        return f"{self.channel_id}:{self.message_ts}"


@dataclass
class Proposal:
    """A proposed JIRA update from the LLM."""
    id: Optional[int] = None
    session_uuid: str = ""
    proposal_id: str = ""  # From LLM output (e.g., "prop-001")

    # JIRA target
    ticket_key: str = ""
    ticket_summary: Optional[str] = None

    # Change details
    change_type: str = ""  # update_field, add_comment, transition, create
    field_name: Optional[str] = None
    current_value: Optional[str] = None
    proposed_value: Optional[str] = None

    # Source tracking
    source: Optional[str] = None  # meeting_transcript, slack_thread
    source_excerpt: Optional[str] = None
    confidence: str = "medium"  # low, medium, high

    # Approval tracking
    status: ProposalStatus = ProposalStatus.PENDING
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None

    # Execution tracking
    executed_at: Optional[datetime] = None
    execution_error: Optional[str] = None

    # Slack message tracking (for updating the message after approval)
    slack_message_ts: Optional[str] = None


@dataclass
class LLMResponse:
    """Structured response from the LLM."""
    session_id: str
    analysis_summary: str
    proposals: list[dict]
    no_action_items: list[dict] = field(default_factory=list)
    error: Optional[str] = None
