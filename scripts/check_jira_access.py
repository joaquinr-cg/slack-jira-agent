#!/usr/bin/env python3
"""Validate Jira API access for this service's configured credentials.

Reads credentials from the shared Jira env vars first:
  - JIRA_SHARED_URL
  - JIRA_SHARED_EMAIL
  - JIRA_SHARED_API_TOKEN

Falls back to the direct Jira env vars used by the LangBuilder components:
  - JIRA_URL
  - JIRA_EMAIL
  - JIRA_API_KEY

Examples:
  python scripts/check_jira_access.py --project LAN
  python scripts/check_jira_access.py --project LAN --issue LAN-123
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any
from urllib.parse import quote, urlparse

import httpx


REQUESTED_PERMISSIONS = [
    "BROWSE_PROJECTS",
    "CREATE_ISSUES",
    "EDIT_ISSUES",
    "ADD_COMMENTS",
    "ASSIGN_ISSUES",
    "TRANSITION_ISSUES",
]
SERVICE_ACCOUNT_DOMAIN = "@serviceaccount.atlassian.com"
API_GATEWAY_HOST = "api.atlassian.com"
TENANT_INFO_PATH = "/_edge/tenant_info"


def _first_env(*names: str) -> tuple[str, str] | tuple[None, None]:
    for name in names:
        value = os.getenv(name)
        if value:
            return name, value
    return None, None


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


def _print_result(label: str, ok: bool, detail: str) -> None:
    status = "OK" if ok else "FAIL"
    print(f"[{status}] {label}: {detail}")


def _response_text(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    try:
        return json.dumps(data, indent=2)
    except TypeError:
        return str(data)


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    **kwargs: Any,
) -> tuple[httpx.Response, Any]:
    response = client.request(method, path, **kwargs)
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return response, response.json()
        except json.JSONDecodeError:
            pass
    return response, response.text


def _build_headers(email: str, api_token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {encoded}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _is_service_account_email(email: str) -> bool:
    return email.lower().endswith(SERVICE_ACCOUNT_DOMAIN)


def _resolve_api_base_url(jira_url: str, jira_email: str, timeout: float) -> str:
    base_url = jira_url.rstrip("/")
    parsed = urlparse(base_url)
    host = parsed.netloc.lower()

    if host == API_GATEWAY_HOST and "/ex/jira/" in parsed.path:
        return base_url

    if not _is_service_account_email(jira_email):
        return base_url

    if not host.endswith(".atlassian.net"):
        raise ValueError(
            "Atlassian service account tokens must use either the Jira site URL "
            "(https://your-site.atlassian.net) or the API gateway URL "
            "(https://api.atlassian.com/ex/jira/<cloudId>)."
        )

    tenant_info_url = f"{base_url}{TENANT_INFO_PATH}"
    response = httpx.get(
        tenant_info_url,
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    cloud_id = response.json().get("cloudId")
    if not cloud_id:
        raise ValueError(
            "Failed to resolve Jira cloudId for the Atlassian service account token."
        )
    return f"https://{API_GATEWAY_HOST}/ex/jira/{cloud_id}"


def _validate_required_env() -> tuple[str, str, str]:
    url_name, jira_url = _first_env("JIRA_SHARED_URL", "JIRA_URL")
    email_name, jira_email = _first_env("JIRA_SHARED_EMAIL", "JIRA_EMAIL")
    token_name, jira_token = _first_env("JIRA_SHARED_API_TOKEN", "JIRA_API_KEY")

    missing = []
    if not jira_url:
        missing.append("JIRA_SHARED_URL or JIRA_URL")
    if not jira_email:
        missing.append("JIRA_SHARED_EMAIL or JIRA_EMAIL")
    if not jira_token:
        missing.append("JIRA_SHARED_API_TOKEN or JIRA_API_KEY")

    if missing:
        print("Missing required environment variables:")
        for item in missing:
            print(f"  - {item}")
        sys.exit(2)

    print("Credential source:")
    print(f"  URL:   {url_name}")
    print(f"  Email: {email_name}")
    print(f"  Token: {token_name} ({_mask_token(jira_token)})")

    return jira_url.rstrip("/"), jira_email, jira_token


def _check_myself(client: httpx.Client) -> bool:
    response, data = _request_json(client, "GET", "/rest/api/3/myself")
    if response.is_success:
        account_type = data.get("accountType", "unknown")
        display_name = data.get("displayName", "unknown")
        active = data.get("active", "unknown")
        _print_result(
            "Authentication",
            True,
            f"displayName={display_name}, accountType={account_type}, active={active}",
        )
        return True

    _print_result(
        "Authentication",
        False,
        f"HTTP {response.status_code} {response.reason_phrase} - {_response_text(data)}",
    )
    return False


def _check_project_list(client: httpx.Client) -> bool:
    response, data = _request_json(
        client,
        "GET",
        "/rest/api/3/project/search",
        params={"maxResults": 50},
    )
    if response.is_success:
        values = data.get("values", [])
        project_keys = [project.get("key", "?") for project in values[:20]]
        detail = f"{len(values)} accessible project(s)"
        if project_keys:
            detail += f" | sample={', '.join(project_keys)}"
        _print_result("Project listing", True, detail)
        return True

    _print_result(
        "Project listing",
        False,
        f"HTTP {response.status_code} {response.reason_phrase} - {_response_text(data)}",
    )
    return False


def _check_project_visibility(client: httpx.Client, project_key: str) -> bool:
    encoded_project = quote(project_key, safe="")
    response, data = _request_json(client, "GET", f"/rest/api/3/project/{encoded_project}")
    if response.is_success:
        _print_result(
            "Project visibility",
            True,
            f"{data.get('key', project_key)} - {data.get('name', 'unknown project name')}",
        )
        return True

    _print_result(
        "Project visibility",
        False,
        f"project={project_key} | HTTP {response.status_code} {response.reason_phrase} - {_response_text(data)}",
    )
    return False


def _check_project_permissions(client: httpx.Client, project_key: str) -> bool:
    response, data = _request_json(
        client,
        "GET",
        "/rest/api/3/mypermissions",
        params={
            "projectKey": project_key,
            "permissions": ",".join(REQUESTED_PERMISSIONS),
        },
    )
    if not response.is_success:
        _print_result(
            "Project permissions",
            False,
            f"project={project_key} | HTTP {response.status_code} {response.reason_phrase} - {_response_text(data)}",
        )
        return False

    permissions = data.get("permissions", {})
    summary = []
    browse_ok = False
    for key in REQUESTED_PERMISSIONS:
        have_permission = bool(permissions.get(key, {}).get("havePermission"))
        if key == "BROWSE_PROJECTS":
            browse_ok = have_permission
        summary.append(f"{key}={'yes' if have_permission else 'no'}")

    _print_result("Project permissions", browse_ok, f"project={project_key} | {'; '.join(summary)}")
    return browse_ok


def _check_issue_search(client: httpx.Client, project_key: str) -> bool:
    response, data = _request_json(
        client,
        "POST",
        "/rest/api/3/search/jql",
        json={
            "jql": f'project = "{project_key}" ORDER BY updated DESC',
            "maxResults": 5,
            "fields": ["summary", "status"],
        },
    )
    if response.is_success:
        issues = data.get("issues", [])
        total = data.get("total", 0)
        issue_keys = [issue.get("key", "?") for issue in issues]
        detail = f"project={project_key} | total={total}"
        if issue_keys:
            detail += f" | sample={', '.join(issue_keys)}"
        else:
            detail += " | no issues returned"
        _print_result("Issue search", True, detail)
        return True

    _print_result(
        "Issue search",
        False,
        f"project={project_key} | HTTP {response.status_code} {response.reason_phrase} - {_response_text(data)}",
    )
    return False


def _check_issue_visibility(client: httpx.Client, issue_key: str) -> bool:
    encoded_issue = quote(issue_key, safe="")
    response, data = _request_json(
        client,
        "GET",
        f"/rest/api/3/issue/{encoded_issue}",
        params={"fields": "summary,status"},
    )
    if response.is_success:
        fields = data.get("fields", {})
        _print_result(
            "Issue visibility",
            True,
            f"{issue_key} - {fields.get('summary', 'no summary returned')}",
        )
        return True

    _print_result(
        "Issue visibility",
        False,
        f"issue={issue_key} | HTTP {response.status_code} {response.reason_phrase} - {_response_text(data)}",
    )
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Jira API access for the configured service account.")
    parser.add_argument(
        "--project",
        help="Project key to validate, for example LAN or PROJ.",
    )
    parser.add_argument(
        "--issue",
        help="Specific issue key to validate, for example LAN-123.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds. Default: 20.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    jira_url, jira_email, jira_token = _validate_required_env()
    api_base_url = _resolve_api_base_url(jira_url, jira_email, args.timeout)

    print(f"Base URL: {jira_url}")
    print(f"API URL:  {api_base_url}")
    print(f"Email:    {jira_email}")
    print()

    headers = _build_headers(jira_email, jira_token)

    all_ok = True
    with httpx.Client(base_url=api_base_url, headers=headers, timeout=args.timeout) as client:
        all_ok &= _check_myself(client)
        all_ok &= _check_project_list(client)

        if args.project:
            print()
            print(f"Checking project {args.project}")
            all_ok &= _check_project_visibility(client, args.project)
            browse_ok = _check_project_permissions(client, args.project)
            all_ok &= browse_ok
            all_ok &= _check_issue_search(client, args.project)

        if args.issue:
            print()
            print(f"Checking issue {args.issue}")
            all_ok &= _check_issue_visibility(client, args.issue)

    print()
    if all_ok:
        print("Result: Jira access checks passed.")
        return 0

    print("Result: One or more Jira access checks failed.")
    print("Most common causes are missing Jira product access, missing Browse Projects permission,")
    print("or issue security rules hiding the requested issues from this service account.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
