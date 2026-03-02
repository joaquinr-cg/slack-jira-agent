"""Configuration management for JIRA Slack Agent."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # ==========================================
    # SLACK CONFIGURATION
    # ==========================================
    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: Optional[str] = None

    # Admin users (comma-separated Slack user IDs)
    admin_user_ids: str = ""

    # Channel for approval messages (optional, defaults to same channel)
    approval_channel_id: Optional[str] = None

    # ==========================================
    # LANGBUILDER CONFIGURATION
    # LangBuilder handles ALL JIRA operations (read AND write)
    # ==========================================
    langbuilder_flow_url: str
    langbuilder_flow_id: str
    langbuilder_api_key: Optional[str] = None

    # ==========================================
    # DATABASE CONFIGURATION
    # ==========================================
    database_path: str = "./data/jira_agent.db"

    # ==========================================
    # AWS / DYNAMODB CONFIGURATION
    # ==========================================
    aws_region: str = "us-east-1"
    dynamodb_table_name: str = "pm_configurations"

    # ==========================================
    # GOOGLE DRIVE (shared service account)
    # These are the defaults for all PMs.
    # PMs can override folder_id and client_email in DynamoDB.
    # ==========================================
    gdrive_project_id: str = ""
    gdrive_client_email: str = ""
    gdrive_private_key: str = ""
    gdrive_private_key_id: str = ""
    gdrive_client_id: str = ""
    gdrive_folder_id: str = ""
    gdrive_folder_name: str = ""
    gdrive_file_filter: str = ""

    # ==========================================
    # TRIGGER FLOW CONFIGURATION
    # Separate LangBuilder flow that checks GDrive for new transcripts.
    # When new transcripts are found, automatically triggers the main
    # jira-sync flow in transcripts_only mode.
    # ==========================================
    trigger_flow_id: Optional[str] = None  # LangBuilder flow ID for transcript detection
    trigger_interval_minutes: int = 10  # How often to check for new transcripts
    trigger_auto_sync: bool = True  # Auto-trigger jira-sync when new transcripts found

    # ==========================================
    # SHARED JIRA SERVICE ACCOUNT (optional)
    # When set, PMs don't need individual JIRA tokens.
    # PMs still provide their own project key(s).
    # ==========================================
    jira_shared_url: Optional[str] = None
    jira_shared_email: Optional[str] = None
    jira_shared_api_token: Optional[str] = None

    # ==========================================
    # SYNC SCHEDULER CONFIGURATION
    # ==========================================
    sync_schedule_check_interval: int = 60  # How often to check schedules (seconds)

    # ==========================================
    # APPLICATION CONFIGURATION
    # ==========================================
    anthropic_api_key: Optional[str] = None  # Optional: enables natural language schedule input
    request_timeout: int = 300  # 5 minutes for LLM processing
    log_level: str = "INFO"

    # Emoji configuration
    mark_emoji: str = "ticket"  # 🎫
    pending_emoji: str = "eyes"  # 👀
    approved_emoji: str = "white_check_mark"  # ✅
    rejected_emoji: str = "x"  # ❌

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def admin_users(self) -> set[str]:
        """Parse admin user IDs from comma-separated string."""
        if not self.admin_user_ids:
            return set()
        return {uid.strip() for uid in self.admin_user_ids.split(",") if uid.strip()}

    def is_admin(self, user_id: str) -> bool:
        """Check if user is an admin. If no admins configured, allow all."""
        if not self.admin_users:
            return True
        return user_id in self.admin_users

    def ensure_data_directory(self) -> None:
        """Ensure the database directory exists."""
        db_path = Path(self.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
