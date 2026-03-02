"""
Slack File Reader Component (Tool Mode)

Extracts text from Slack file attachments that were pre-downloaded by the bot.
The bot downloads the file from Slack (handling auth), base64-encodes it,
and passes the content via tweaks. This component decodes and extracts text.

Supports PDF, DOCX, plain text, CSV, JSON, and XML files.

Architecture:
Bot (downloads from Slack) → base64 via tweaks → SlackFileReader → text extraction
"""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Any

from loguru import logger

from langbuilder.custom.custom_component.component import Component
from langbuilder.io import (
    MessageTextInput,
    MultilineInput,
    Output,
)
from langbuilder.schema.message import Message


class SlackFileReaderComponent(Component):
    """Extract text from Slack file uploads.

    File content is pre-downloaded by the bot and passed as base64 via tweaks.
    Supports PDF, DOCX, plain text, CSV, JSON, and XML.
    """

    display_name = "Slack File Reader"
    description = "Extract text from Slack file uploads (PDF, DOCX, TXT, CSV). The file content is pre-loaded by the bot. Call this tool when the user's message mentions an attached file."
    icon = "FileText"
    name = "SlackFileReader"

    inputs = [
        # === Pre-loaded content (injected via tweaks by the bot) ===
        MultilineInput(
            name="file_content_b64",
            display_name="File Content (base64)",
            info="Base64-encoded file content, injected by the bot via tweaks.",
            required=False,
            advanced=True,
        ),
        # === Metadata (injected via tweaks AND/OR set by agent) ===
        MessageTextInput(
            name="filename",
            display_name="Filename",
            info="Original filename (e.g., 'proposal.docx').",
            required=False,
            tool_mode=True,
        ),
        MessageTextInput(
            name="mimetype",
            display_name="MIME Type",
            info="File MIME type (e.g., 'application/pdf'). Will guess from filename if empty.",
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
        """Decode base64 file content and extract text."""
        content_b64 = self.file_content_b64
        filename = self.filename or "unknown"
        mimetype = self.mimetype or self._guess_mimetype(filename)

        if not content_b64:
            return Message(text=json.dumps({
                "success": False,
                "error": "No file content available. The file may not have been uploaded with this message.",
            }))

        try:
            file_bytes = base64.b64decode(content_b64)
            logger.info(f"Decoding file {filename}: {len(file_bytes)} bytes")

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
