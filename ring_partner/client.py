"""Async client for the Ring Partner API (https://api.amazonvision.com).

Ring ships no official SDK. This wraps the documented REST surface: device
discovery, status, capabilities, event history, image snapshots, video clips
and WHEP live view. Unofficial; not affiliated with Ring or Amazon.

Two auth modes, matching Ring's hello-world sample:

* access token: a short-lived token pasted from the Ring Developer Playground.
* refresh token: OAuth client credentials plus a refresh token, auto-renewed.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

RING_API_BASE = "https://api.amazonvision.com"
RING_OAUTH_URL = "https://oauth.ring.com/oauth/token"


class RingError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class RingAuthError(RingError):
    """No usable token, or Ring rejected it (expired Playground tokens land here)."""


class MediaNotReady(RingError):
    """No media for that range: 416 MEDIA_NOT_FOUND, 425 RECORDING_NOT_READY, or a 403 saying the
    requested time range is outside what the app is authorized for."""


@dataclass
class Device:
    id: str
    name: str
    kind: str
    raw: dict[str, Any]


@dataclass
class HistoryEvent:
    id: str
    device_id: str
    event_type: str
    start_ms: int
    end_ms: int | None
    raw: dict[str, Any]


@dataclass
class Media:
    content: bytes
    content_type: str


def _as_ms(value: Any) -> int | None:
    """History timestamps are documented as epoch ms but arrive as numbers or numeric strings."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _error_from(resp: httpx.Response) -> RingError:
    code = None
    message = resp.text[:300]
    try:
        body = resp.json()
        first = (body.get("errors") or [{}])[0]
        code = first.get("code") or body.get("code")
        message = first.get("detail") or first.get("title") or body.get("message") or message
    except (ValueError, AttributeError, IndexError):
        pass
    text = f"Ring API {resp.status_code}: {message}"
    if resp.status_code == 403 and "time range" in str(message).lower():
        # Seen on the Developer Playground: media is only served for times the app is authorized
        # for (after consent, around events). This is "no media for that range", not a bad token.
        return MediaNotReady(text, status=403, code=code or "TIME_RANGE_NOT_AUTHORIZED")
    if resp.status_code in (401, 403):
        return RingAuthError(text, status=resp.status_code, code=code)
    if resp.status_code in (416, 425):
        return MediaNotReady(text, status=resp.status_code, code=code)
    return RingError(text, status=resp.status_code, code=code)


class RingClient:
    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        api_base: str = RING_API_BASE,
        oauth_url: str = RING_OAUTH_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.oauth_url = oauth_url
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._expires_at = 0.0
        # Media downloads answer 303 with a pre-signed Location; we follow it by hand
        # so the bearer token is never forwarded to the download host.
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- auth ------------------------------------------------------------

    @property
    def auth_mode(self) -> str | None:
        if self._refresh_token:
            return "refresh_token"
        if self._access_token:
            return "access_token"
        return None

    def set_access_token(self, token: str | None) -> None:
        """Swap in a fresh Playground token at runtime (they last about 30 minutes)."""
        self._access_token = token or None
        if not self._refresh_token:
            self._expires_at = 0.0

    async def _token(self) -> str:
        if self._refresh_token:
            async with self._lock:
                if self._access_token and time.time() < self._expires_at:
                    return self._access_token
                if not (self._client_id and self._client_secret):
                    raise RingAuthError("RING_CLIENT_ID and RING_CLIENT_SECRET are required with a refresh token")
                resp = await self._http.post(
                    self.oauth_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
                if resp.status_code != 200:
                    raise RingAuthError(f"Token refresh failed ({resp.status_code})", status=resp.status_code)
                data = resp.json()
                self._access_token = data["access_token"]
                self._refresh_token = data.get("refresh_token", self._refresh_token)
                self._expires_at = time.time() + float(data.get("expires_in", 3600)) - 60
                return self._access_token
        if self._access_token:
            return self._access_token
        raise RingAuthError("No Ring token configured. Paste one from the Ring Developer Playground.")

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.api_base}{path}"

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._http.get(self._url(path), params=params, headers=await self._headers())
        if resp.status_code != 200:
            raise _error_from(resp)
        return resp.json()

    # -- devices ---------------------------------------------------------

    async def list_devices(self) -> list[Device]:
        body = await self._get_json("/v1/devices")
        devices = []
        for item in body.get("data") or []:
            attrs = item.get("attributes") or {}
            devices.append(
                Device(
                    id=item["id"],
                    name=attrs.get("name") or attrs.get("description") or "Ring device",
                    kind=attrs.get("kind") or attrs.get("device_type") or item.get("type") or "device",
                    raw=item,
                )
            )
        return devices

    async def device_status(self, device_id: str) -> dict[str, Any]:
        body = await self._get_json(f"/v1/devices/{device_id}/status")
        return (body.get("data") or {}).get("attributes") or {}

    async def device_capabilities(self, device_id: str) -> dict[str, Any]:
        body = await self._get_json(f"/v1/devices/{device_id}/capabilities")
        return (body.get("data") or {}).get("attributes") or {}

    async def user_profile(self) -> dict[str, Any]:
        body = await self._get_json("/v1/users/me")
        return body.get("data") or {}

    # -- event history ---------------------------------------------------

    async def event_history(
        self,
        device_id: str,
        *,
        event_types: str | None = None,
        max_pages: int = 3,
    ) -> list[HistoryEvent]:
        """Newest-first event metadata. Follows links.next for up to max_pages pages."""
        events: list[HistoryEvent] = []
        path: str | None = f"/v1/history/devices/{device_id}/events"
        params: dict[str, Any] | None = {"event_types": event_types} if event_types else None
        for _ in range(max_pages):
            if not path:
                break
            body = await self._get_json(path, params)
            params = None  # links.next already carries the filters and cursor
            page = body.get("data") or []
            for item in page:
                attrs = item.get("attributes") or {}
                start = _as_ms(attrs.get("start"))
                if start is None:
                    continue
                source = ((item.get("relationships") or {}).get("source") or {}).get("data") or {}
                events.append(
                    HistoryEvent(
                        id=str(item.get("id")),
                        device_id=source.get("id") or device_id,
                        event_type=attrs.get("event_type") or "unknown",
                        start_ms=start,
                        end_ms=_as_ms(attrs.get("end")),
                        raw=item,
                    )
                )
            path = (body.get("links") or {}).get("next") if page else None
        return events

    # -- media -----------------------------------------------------------

    async def _download(self, path: str, payload: dict[str, Any]) -> Media:
        resp = await self._http.post(
            self._url(path),
            json=payload,
            headers=await self._headers({"Content-Type": "application/json"}),
        )
        if resp.status_code in (301, 302, 303, 307):
            location = resp.headers.get("Location")
            if not location:
                raise RingError("Media redirect had no Location header", status=resp.status_code)
            if location.startswith("/"):
                location = f"{self.api_base}{location}"
            # The Location URL is pre-signed: fetch it without our Authorization header.
            resp = await self._http.get(location, follow_redirects=True)
        if resp.status_code not in (200, 206):
            raise _error_from(resp)
        return Media(content=resp.content, content_type=resp.headers.get("Content-Type", "application/octet-stream"))

    async def snapshot_at(
        self,
        device_id: str,
        timestamp_ms: int,
        *,
        fmt: str = "jpeg",
        width: int | None = None,
        height: int | None = None,
        component_id: str | None = None,
    ) -> Media:
        """The frame at one moment (image download, type=at_timestamp)."""
        payload: dict[str, Any] = {"type": "at_timestamp", "timestamp": int(timestamp_ms)}
        payload["image_options"] = _image_options(fmt, width, height)
        if component_id is not None:
            payload["components"] = [{"component_id": str(component_id)}]
        return await self._download(f"/v1/devices/{device_id}/media/image/download", payload)

    async def latest_snapshot(
        self,
        device_id: str,
        start_ms: int,
        end_ms: int | None = None,
        *,
        fmt: str = "jpeg",
        width: int | None = None,
        height: int | None = None,
        component_id: str | None = None,
    ) -> Media:
        """The most recent frame inside a window of at most 24 hours (type=latest_in_range)."""
        payload: dict[str, Any] = {"type": "latest_in_range", "start_timestamp": int(start_ms)}
        if end_ms is not None:
            payload["end_timestamp"] = int(end_ms)
        payload["image_options"] = _image_options(fmt, width, height)
        if component_id is not None:
            payload["components"] = [{"component_id": str(component_id)}]
        return await self._download(f"/v1/devices/{device_id}/media/image/download", payload)

    async def video_clip(
        self,
        device_id: str,
        timestamp_ms: int,
        duration_ms: int,
        *,
        retry_until: float | None = None,
        component_id: str | None = None,
    ) -> Media:
        """Recorded footage as MP4.

        This endpoint never starts a recording. For a recent event Ring's guidance is
        to retry the unchanged request on 416/425 with backoff, and give up two minutes
        after the requested range ends; pass retry_until (a time.time() deadline) for that.
        """
        payload: dict[str, Any] = {"timestamp": int(timestamp_ms), "duration": int(duration_ms)}
        if component_id is not None:
            payload["components"] = [{"component_id": str(component_id)}]
        delays = [5, 10, 20, 30]
        attempt = 0
        while True:
            try:
                return await self._download(f"/v1/devices/{device_id}/media/video/download", payload)
            except MediaNotReady as exc:
                if exc.status == 403:
                    raise  # an unauthorized range will not become available by waiting
                delay = delays[min(attempt, len(delays) - 1)] + random.uniform(0, 1.5)
                if retry_until is None or time.time() + delay > retry_until:
                    raise
                attempt += 1
                await asyncio.sleep(delay)

    # -- live view (WHEP) --------------------------------------------------

    async def whep_start(self, device_id: str, sdp_offer: str) -> tuple[str, str | None]:
        """Trade a WebRTC SDP offer for Ring's answer. Returns (sdp_answer, session_url)."""
        resp = await self._http.post(
            self._url(f"/v1/devices/{device_id}/media/streaming/whep/sessions"),
            content=sdp_offer.encode(),
            headers=await self._headers({"Content-Type": "application/sdp"}),
        )
        if resp.status_code not in (200, 201):
            raise _error_from(resp)
        location = resp.headers.get("Location")
        if location and location.startswith("/"):
            location = f"{self.api_base}{location}"
        return resp.text, location

    async def whep_stop(self, session_url: str) -> None:
        host = httpx.URL(session_url).host
        if not (session_url.startswith(self.api_base) or host.endswith(".amazonvision.com")):
            raise RingError("Refusing to send the Ring token to a non-Ring session URL")
        await self._http.delete(session_url, headers=await self._headers())


def _image_options(fmt: str, width: int | None, height: int | None) -> dict[str, Any]:
    options: dict[str, Any] = {"format": fmt}
    if width and height:
        options["resolution"] = {"width": int(width), "height": int(height)}
    return options
