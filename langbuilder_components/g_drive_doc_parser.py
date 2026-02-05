"""
Google Drive Docs Parser (Service Account) - Custom Component

Connects to Google Drive using service account credentials,
finds the latest Google Doc in a shared folder, and extracts its content.
"""

from langbuilder.custom.custom_component.component import Component
from langbuilder.io import MessageTextInput, MultilineInput, Output
from langbuilder.schema.data import Data
from langbuilder.schema.message import Message


class GoogleDriveDocsParserSA(Component):
    display_name = "Google Drive Docs Parser (Service Account)"
    description = "Parse the latest Google Doc from a Google Drive folder using service account."
    icon = "file-text"
    name = "GoogleDriveDocsParserSA"

    inputs = [
        MessageTextInput(
            name="project_id",
            display_name="Project ID",
            info="Google Cloud project ID",
            required=True,
        ),
        MessageTextInput(
            name="client_email",
            display_name="Client Email",
            info="Service account email (ends with .iam.gserviceaccount.com)",
            required=True,
        ),
        MultilineInput(
            name="private_key",
            display_name="Private Key",
            info="Private key - paste the entire key including BEGIN/END lines",
            required=True,
        ),
        MessageTextInput(
            name="private_key_id",
            display_name="Private Key ID",
            info="Private key ID",
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="client_id",
            display_name="Client ID (numbers)",
            info="Client ID (numeric)",
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="folder_name",
            display_name="Folder Name",
            info="Name of the Google Drive folder (must be shared with service account email).",
            value="Meet recordings",
            required=True,
        ),
        MessageTextInput(
            name="folder_id",
            display_name="Folder ID (Optional)",
            info="Direct folder ID - overrides folder name if provided.",
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="file_filter",
            display_name="File Name Filter",
            info="Optional: filter files by name (contains match).",
            required=False,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Document Content", name="content", method="parse_document"),
        Output(display_name="Document Data", name="data", method="get_document_data"),
    ]

    def _get_drive_service(self):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        private_key = self.private_key

        if "\\n" in private_key and "\n" not in private_key:
            private_key = private_key.replace("\\n", "\n")

        client_email_encoded = self.client_email.replace("@", "%40")

        creds_dict = {
            "type": "service_account",
            "project_id": self.project_id,
            "private_key_id": self.private_key_id or "",
            "private_key": private_key,
            "client_email": self.client_email,
            "client_id": self.client_id or "",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": (
                f"https://www.googleapis.com/robot/v1/metadata/x509/{client_email_encoded}"
            ),
            "universe_domain": "googleapis.com",
        }

        credentials = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )

        return build("drive", "v3", credentials=credentials)

    def _find_folder_id(self, service, folder_name: str) -> str:
        query = (
            f"name = '{folder_name}' and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )

        results = (
            service.files()
            .list(
                q=query,
                pageSize=10,
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

        folders = results.get("files", [])
        if not folders:
            raise ValueError(
                f"Folder '{folder_name}' not found. Ensure it's shared with: {self.client_email}"
            )

        return folders[0]["id"]

    def _get_latest_google_doc(self, service, folder_id: str, name_filter: str = "") -> dict:
        google_doc_mime = "application/vnd.google-apps.document"
        query = (
            f"'{folder_id}' in parents and "
            f"mimeType = '{google_doc_mime}' and "
            "trashed = false"
        )

        if name_filter:
            query += f" and name contains '{name_filter}'"

        results = (
            service.files()
            .list(
                q=query,
                pageSize=1,
                orderBy="modifiedTime desc",
                fields="files(id, name, modifiedTime)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

        files = results.get("files", [])
        if not files:
            msg = "No Google Docs found in folder"
            if name_filter:
                msg += f" matching '{name_filter}'"
            raise ValueError(msg)

        return files[0]

    def _export_google_doc(self, service, file_id: str) -> str:
        content = (
            service.files()
            .export(
                fileId=file_id,
                mimeType="text/plain",
            )
            .execute()
        )

        if isinstance(content, bytes):
            return content.decode("utf-8")

        return str(content)

    def _execute(self) -> dict:
        service = self._get_drive_service()

        if self.folder_id and self.folder_id.strip():
            folder_id = self.folder_id.strip()
        else:
            folder_id = self._find_folder_id(service, self.folder_name)

        file_filter = self.file_filter if self.file_filter else ""
        file_info = self._get_latest_google_doc(service, folder_id, file_filter)
        content = self._export_google_doc(service, file_info["id"])

        return {
            "text": content,
            "file_name": file_info["name"],
            "file_id": file_info["id"],
            "modified_time": file_info["modifiedTime"],
            "folder_id": folder_id,
            "folder_name": self.folder_name,
        }

    def parse_document(self) -> Message:
        """Return the document content as a Message (same type as AgentComponent output)."""
        result = self._execute()
        self.status = f"Parsed: {result['file_name']}"
        return Message(text=result["text"])

    def get_document_data(self) -> Data:
        result = self._execute()
        self.status = f"Parsed: {result['file_name']}"
        return Data(data=result, text=result["text"])
