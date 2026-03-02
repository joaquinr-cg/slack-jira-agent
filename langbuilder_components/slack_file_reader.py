"""
Slack File Reader Component (Tool Mode)

Downloads and extracts text from Slack file attachments.
Supports PDF, DOCX, plain text, CSV, JSON, and XML files.

Used as a tool by AI agents to read files uploaded by users in Slack.
The bot_token is injected at runtime via tweaks — the agent only needs
to provide file_url, filename, and mimetype.

Architecture:
Agent → Slack File Reader → Slack Files API → text extraction
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

import httpx
from loguru import logger

from langbuilder.custom.custom_component.component import Component
from langbuilder.io import (
    MessageTextInput,
    Output,
    SecretStrInput,
)
from langbuilder.schema.message import Message


class SlackFileReaderComponent(Component):
    """Download and extract text from Slack file uploads.

    Supports PDF, DOCX, plain text, CSV, JSON, and XML.
    The bot_token is injected via tweaks at runtime.
    """

    display_name = "Slack File Reader"
    description = "Download and extract text from Slack file uploads (PDF, DOCX, TXT, CSV). Use this when the user's message contains [Attached file: ...] metadata."
    icon = "FileText"
    name = "SlackFileReader"

    inputs = [
        # === Auth (injected via tweaks, NOT set by agent) ===
        SecretStrInput(
            name="bot_token",
            display_name="Slack Bot Token",
            info="Slack bot token (xoxb-...) for downloading files. Injected via tweaks at runtime.",
            required=False,
        ),
        # === Tool-mode inputs (agent sets these) ===
        MessageTextInput(
            name="file_url",
            display_name="File URL",
            info="The file download URL from the [Attached file: ...] metadata in the user's message.",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="filename",
            display_name="Filename",
            info="Original filename (e.g., 'proposal.docx') from the attachment metadata.",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="mimetype",
            display_name="MIME Type",
            info="File MIME type from the attachment metadata (e.g., 'application/pdf'). Optional — will guess from filename if empty.",
            required=False,
            value="",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            display_name="Extracted Text",
            name="extracted_text",
            method="read_file",
        ),
    ]

    def read_file(self) -> Message:
        """Download the file from Slack and extract text content."""
        bot_token = self.bot_token or os.environ.get("SLACK_BOT_TOKEN", "")
        file_url = self.file_url
        filename = self.filename
        mimetype = self.mimetype or self._guess_mimetype(filename)

        if not bot_token:
            return Message(text=json.dumps({
                "success": False,
                "error": "No Slack bot token configured. Ensure SLACK_BOT_TOKEN is set.",
            }))

        if not file_url:
            return Message(text=json.dumps({
                "success": False,
                "error": "No file URL provided.",
            }))

        try:
            file_bytes = self._download(file_url, bot_token)
            logger.info(f"Downloaded {filename}: {len(file_bytes)} bytes")

            text = self._extract_text(file_bytes, mimetype, filename)

            return Message(text=json.dumps({
                "success": True,
                "filename": filename,
                "mimetype": mimetype,
                "char_count": len(text),
                "content": text,
            }))

        except Exception as e:
            logger.error(f"Failed to process file {filename}: {e}")
            return Message(text=json.dumps({
                "success": False,
                "filename": filename,
                "error": str(e),
            }))

    def _download(self, url: str, token: str) -> bytes:
        """Download file from Slack, manually following redirects to preserve auth."""
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=60.0) as client:
            response = client.get(url, headers=headers, follow_redirects=False)
            max_redirects = 5
            while response.is_redirect and max_redirects > 0:
                redirect_url = response.headers.get("location", "")
                response = client.get(redirect_url, headers=headers, follow_redirects=False)
                max_redirects -= 1
            response.raise_for_status()
            return response.content

    def _extract_text(self, file_bytes: bytes, mimetype: str, filename: str) -> str:
        """Extract text from file bytes based on MIME type."""
        if mimetype == "application/pdf" or filename.lower().endswith(".pdf"):
            return self._extract_pdf(file_bytes, filename)

        if mimetype in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ) or filename.lower().endswith((".docx", ".doc")):
            return self._extract_docx(file_bytes, filename)

        if mimetype.startswith("text/") or mimetype in (
            "application/csv", "application/json", "application/xml",
        ) or filename.lower().endswith((".txt", ".md", ".csv", ".json", ".xml")):
            return self._extract_plain_text(file_bytes, filename)

        if mimetype.startswith("image/"):
            return f"[Image file: {filename} — text extraction not supported]"

        return f"[Unsupported file type: {filename} ({mimetype})]"

    def _extract_pdf(self, file_bytes: bytes, filename: str) -> str:
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            pages = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    pages.append(f"--- Page {i + 1} ---\n{text}")
            if not pages:
                return f"[PDF: {filename} — no extractable text (possibly scanned)]"
            return "\n\n".join(pages)
        except Exception as e:
            logger.error(f"PDF extraction failed for {filename}: {e}")
            return f"[PDF extraction failed: {filename} — {str(e)}]"

    def _extract_docx(self, file_bytes: bytes, filename: str) -> str:
        try:
            from docx import Document
            doc = Document(io.BytesIO(file_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            if not paragraphs:
                return f"[DOCX: {filename} — no extractable text]"
            return "\n\n".join(paragraphs)
        except Exception as e:
            logger.error(f"DOCX extraction failed for {filename}: {e}")
            return f"[DOCX extraction failed: {filename} — {str(e)}]"

    def _extract_plain_text(self, file_bytes: bytes, filename: str) -> str:
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return file_bytes.decode("latin-1")
            except Exception as e:
                return f"[Text decoding failed: {filename} — {str(e)}]"

    def _guess_mimetype(self, filename: str) -> str:
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        return {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "doc": "application/msword",
            "txt": "text/plain",
            "md": "text/markdown",
            "csv": "text/csv",
            "json": "application/json",
            "xml": "application/xml",
        }.get(ext, "application/octet-stream")
