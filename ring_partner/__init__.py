"""Unofficial async Python client for the Ring Partner API. Not affiliated with Ring or Amazon."""

from .client import (
    RING_API_BASE,
    Device,
    HistoryEvent,
    Media,
    MediaNotReady,
    RingAuthError,
    RingClient,
    RingError,
)
from .webhooks import WebhookError, WebhookEvent, parse, sign, verify_signature

__version__ = "0.1.0"

__all__ = [
    "RING_API_BASE",
    "Device",
    "HistoryEvent",
    "Media",
    "MediaNotReady",
    "RingAuthError",
    "RingClient",
    "RingError",
    "WebhookError",
    "WebhookEvent",
    "parse",
    "sign",
    "verify_signature",
]
