"""Helpers for resolving Jira tenant metadata and API base URLs."""

from __future__ import annotations

import logging
from functools import lru_cache
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

SERVICE_ACCOUNT_DOMAIN = "@serviceaccount.atlassian.com"
API_GATEWAY_HOST = "api.atlassian.com"
TENANT_INFO_PATH = "/_edge/tenant_info"
DEFAULT_TIMEOUT_SECONDS = 10.0


def is_service_account_email(email: str) -> bool:
    """Return True when the credentials belong to an Atlassian service account."""
    return email.lower().endswith(SERVICE_ACCOUNT_DOMAIN)


@lru_cache(maxsize=32)
def resolve_jira_cloud_id(jira_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Resolve Atlassian cloudId from a Jira site URL."""
    base_url = jira_url.rstrip("/")
    parsed = urlparse(base_url)
    host = parsed.netloc.lower()

    if host == API_GATEWAY_HOST and "/ex/jira/" in parsed.path:
        return parsed.path.rstrip("/").split("/")[-1]

    if not host.endswith(".atlassian.net"):
        raise ValueError(
            "Jira URL must be an Atlassian site URL like https://your-site.atlassian.net "
            "or an API gateway URL like https://api.atlassian.com/ex/jira/<cloudId>."
        )

    tenant_info_url = f"{base_url}{TENANT_INFO_PATH}"
    try:
        response = httpx.get(
            tenant_info_url,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ValueError(
            f"Failed to resolve Jira cloudId from {tenant_info_url}: {exc}"
        ) from exc

    cloud_id = response.json().get("cloudId")
    if not cloud_id:
        raise ValueError(f"No cloudId returned from {tenant_info_url}")

    return cloud_id


def build_jira_api_base_url(
    jira_url: str,
    email: str,
    cloud_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Build the API base URL to use for Jira REST calls."""
    if not jira_url or not email or not is_service_account_email(email):
        return None

    parsed = urlparse(jira_url.rstrip("/"))
    if parsed.netloc.lower() == API_GATEWAY_HOST and "/ex/jira/" in parsed.path:
        return jira_url.rstrip("/")

    effective_cloud_id = cloud_id or resolve_jira_cloud_id(jira_url, timeout)
    api_base_url = f"https://{API_GATEWAY_HOST}/ex/jira/{effective_cloud_id}"
    logger.info("Resolved Jira API gateway URL for %s: %s", jira_url, api_base_url)
    return api_base_url
