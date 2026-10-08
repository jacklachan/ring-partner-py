"""Ring webhook verification and parsing (payload format v1.1).

Ring signs every webhook with HMAC-SHA256 over the raw request body and sends
the hex digest in the X-Signature header as ``sha256=<hex>``. Verify first,
parse second: re-serialising the JSON changes the bytes and breaks the digest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


class WebhookError(Exception):
    pass


def sign(signing_key: str, raw_body: bytes) -> str:
    """The X-Signature header value Ring would send for this body."""
    return "sha256=" + hmac.new(signing_key.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(signing_key: str, raw_body: bytes, received: str | None) -> bool:
    if not received:
        return False
    expected = hmac.new(signing_key.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.removeprefix("sha256=").strip())


@dataclass
class WebhookEvent:
    request_id: str
    account_id: str | None
    event_id: str
    type: str
    sub_type: str | None
    device_id: str | None
    timestamp_ms: int | None
    component_ids: list[str]
    raw: dict[str, Any]

    @property
    def is_device_event(self) -> bool:
        return self.type in {"motion_detected", "button_press"}


def parse(raw_body: bytes) -> WebhookEvent:
    try:
        payload = json.loads(raw_body)
        meta = payload["meta"]
        data = payload["data"]
        attrs = data.get("attributes") or {}
    except (ValueError, KeyError, TypeError) as exc:
        raise WebhookError("Not a Ring v1.1 webhook payload") from exc
    if not meta.get("request_id") or not data.get("type"):
        raise WebhookError("Webhook is missing meta.request_id or data.type")

    timestamp = attrs.get("timestamp")
    try:
        timestamp_ms = int(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        timestamp_ms = None

    source_type = attrs.get("source_type")
    device_id = attrs.get("source") if source_type == "devices" else None
    return WebhookEvent(
        request_id=str(meta["request_id"]),
        account_id=meta.get("account_id"),
        event_id=str(data.get("id") or meta["request_id"]),
        type=str(data["type"]),
        sub_type=attrs.get("sub_type"),
        device_id=device_id,
        timestamp_ms=timestamp_ms,
        component_ids=[str(c) for c in attrs.get("component_ids") or []],
        raw=payload,
    )
