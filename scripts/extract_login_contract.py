"""Extract a value-free login API contract from a browser HAR file.

The output contains request paths and field names only.  It deliberately
omits header values, cookie values, query values, request bodies, response
values, and response bodies so a HAR can be inspected without exposing a
phone number, OTP, captcha, session cookie, or Web-Token.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit


AUTH_HINT = re.compile(
    r"(?:login|auth|captcha|verify|verification|sms|otp|token|mobile|phone|"
    r"check.?code|validate.?code|send.?code|login.?type)",
    re.IGNORECASE,
)
SENSITIVE_PATH_SEGMENT = re.compile(
    r"(?:\d{4,}|[0-9a-f]{8}-[0-9a-f-]{20,}|[A-Za-z0-9_.-]{25,})",
    re.IGNORECASE,
)


def _field_paths(value: Any, prefix: str = "", *, depth: int = 0) -> set[str]:
    """Return JSON field paths without retaining any values."""
    if depth > 5:
        return set()
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            paths.add(name)
            paths.update(_field_paths(child, name, depth=depth + 1))
        return paths
    if isinstance(value, list) and value:
        name = f"{prefix}[]" if prefix else "[]"
        return {name, *_field_paths(value[0], name, depth=depth + 1)}
    return set()


def _decoded_content(content: dict[str, Any]) -> str:
    text = content.get("text")
    if not isinstance(text, str):
        return ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return text


def _json_fields(text: str) -> list[str]:
    try:
        return sorted(_field_paths(json.loads(text)))
    except (TypeError, ValueError):
        return []


def _request_body_fields(post_data: dict[str, Any]) -> list[str]:
    names = {
        str(param.get("name"))
        for param in post_data.get("params") or []
        if isinstance(param, dict) and param.get("name")
    }
    text = post_data.get("text")
    mime = str(post_data.get("mimeType") or "").lower()
    if isinstance(text, str):
        if "json" in mime:
            names.update(_json_fields(text))
        elif "x-www-form-urlencoded" in mime:
            names.update(name for name, _ in parse_qsl(text, keep_blank_values=True))
    return sorted(names)


def _safe_path(path: str) -> str:
    parts = []
    for raw in path.split("/"):
        segment = unquote(raw)
        parts.append("{redacted}" if SENSITIVE_PATH_SEGMENT.fullmatch(segment) else raw)
    return "/".join(parts)


def _names(items: Iterable[Any]) -> list[str]:
    return sorted({
        str(item.get("name")).lower()
        for item in items
        if isinstance(item, dict) and item.get("name")
    })


def _entry_contract(entry: dict[str, Any], index: int) -> dict[str, Any] | None:
    request = entry.get("request")
    response = entry.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        return None

    parsed = urlsplit(str(request.get("url") or ""))
    post_data = request.get("postData") if isinstance(request.get("postData"), dict) else {}
    response_content = response.get("content") if isinstance(response.get("content"), dict) else {}
    return {
        "entry": index,
        "method": str(request.get("method") or ""),
        "origin": f"{parsed.scheme}://{parsed.netloc}",
        "path": _safe_path(parsed.path),
        "query_fields": sorted({name for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}),
        "request_header_fields": _names(request.get("headers") or []),
        "request_cookie_fields": _names(request.get("cookies") or []),
        "request_body_mime": str(post_data.get("mimeType") or ""),
        "request_body_fields": _request_body_fields(post_data),
        "response_status": response.get("status"),
        "response_header_fields": _names(response.get("headers") or []),
        "response_cookie_fields": _names(response.get("cookies") or []),
        "response_body_mime": str(response_content.get("mimeType") or ""),
        "response_body_fields": _json_fields(_decoded_content(response_content)),
    }


def _looks_like_auth(item: dict[str, Any]) -> bool:
    searchable = " ".join([
        item["path"],
        *item["query_fields"],
        *item["request_body_fields"],
        *item["response_body_fields"],
    ])
    return bool(AUTH_HINT.search(searchable))


def extract_contract(har: dict[str, Any], *, host: str, include_all: bool = False) -> dict[str, Any]:
    entries = ((har.get("log") or {}).get("entries") or []) if isinstance(har, dict) else []
    contracts = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        item = _entry_contract(entry, index)
        if item is None or urlsplit(item["origin"]).hostname != host:
            continue
        if include_all or _looks_like_auth(item):
            contracts.append(item)
    return {
        "format": "npedi-login-contract-v1",
        "host": host,
        "contains_values": False,
        "entries": contracts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har", type=Path, help="HAR exported from browser DevTools")
    parser.add_argument("--host", default="www.npedi.com")
    parser.add_argument("--all-host-requests", action="store_true", help="include every request to --host")
    parser.add_argument("--output", type=Path, help="write sanitized JSON here; stdout when omitted")
    args = parser.parse_args()

    har = json.loads(args.har.read_text(encoding="utf-8-sig"))
    result = extract_contract(har, host=args.host, include_all=args.all_host_requests)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
