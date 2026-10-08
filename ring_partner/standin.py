"""A local stand-in for the Ring Partner API.

This is NOT Ring and its frames are NOT Ring footage. It exists so your test
suite and offline development can exercise real client code paths (JSON:API
device list, event history, the 303 image-download flow, HMAC-signed webhooks)
without a Playground token or network access. Frames are drawn with Pillow and
carry their ground-truth labels in a JPEG comment (see read_labels), so tests
can assert on what a frame "shows" without a vision model.

Needs the "standin" extra: pip install "ring-partner[standin]".
"""

from __future__ import annotations

import io
import json
import uuid
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageDraw

from . import webhooks

DEVICE_ID = "ava1.ring.device.SIMULATED-DOORBELL"
ACCOUNT_ID = "ava1.ring.account.SIMULATED"
LABEL_MARKER = "ring-partner-standin:"

SCENES: dict[str, dict[str, Any]] = {
    "empty": {"package": False, "person": False, "vehicle": False, "summary": "The porch is empty."},
    "package": {
        "package": True,
        "person": False,
        "vehicle": False,
        "summary": "A cardboard package is sitting on the doormat.",
    },
    "delivery": {
        "package": True,
        "person": True,
        "vehicle": True,
        "summary": "A courier is at the door with a package; a van is parked at the kerb.",
    },
    "vehicle": {
        "package": False,
        "person": False,
        "vehicle": True,
        "summary": "A van is parked at the kerb. Nothing on the porch.",
    },
    "visitor": {
        "package": False,
        "person": True,
        "vehicle": False,
        "summary": "A person is standing at the door.",
    },
    "pickup": {
        "package": False,
        "person": True,
        "vehicle": False,
        "summary": "Someone is at the open door; the doormat is clear.",
    },
}


def render_scene(scene: str, caption: str = "") -> bytes:
    """Draw a simple porch. Figures are faceless on purpose."""
    spec = SCENES[scene]
    w, h = 960, 540
    img = Image.new("RGB", (w, h), (188, 205, 214))
    d = ImageDraw.Draw(img)

    # street and kerb
    d.rectangle([0, 150, w, 250], fill=(120, 126, 130))
    d.rectangle([0, 250, w, 268], fill=(170, 172, 170))
    # lawn and path
    d.rectangle([0, 268, w, h], fill=(126, 160, 112))
    d.polygon([(380, 268), (580, 268), (700, h), (260, h)], fill=(196, 188, 172))

    if spec["vehicle"]:
        d.rounded_rectangle([560, 120, 900, 236], radius=14, fill=(238, 238, 232), outline=(90, 90, 90), width=3)
        d.rectangle([800, 138, 880, 190], fill=(120, 150, 170))
        d.rectangle([590, 150, 760, 200], fill=(214, 120, 60))
        for cx in (640, 840):
            d.ellipse([cx - 26, 210, cx + 26, 262], fill=(40, 40, 40))
            d.ellipse([cx - 10, 226, cx + 10, 246], fill=(150, 150, 150))

    # doormat
    d.polygon([(390, 430), (570, 430), (610, 500), (350, 500)], fill=(112, 84, 62))

    if spec["package"]:
        d.polygon([(430, 400), (530, 400), (545, 470), (415, 470)], fill=(190, 148, 96), outline=(120, 90, 50))
        d.polygon([(430, 400), (530, 400), (520, 378), (440, 378)], fill=(206, 166, 112), outline=(120, 90, 50))
        d.line([(480, 378), (480, 470)], fill=(150, 112, 66), width=5)

    if spec["person"]:
        x = 640 if spec["package"] else 500
        d.ellipse([x - 26, 232, x + 26, 284], fill=(84, 92, 110))
        d.rounded_rectangle([x - 44, 284, x + 44, 420], radius=20, fill=(60, 84, 128))
        d.rectangle([x - 36, 420, x - 6, 492], fill=(50, 54, 66))
        d.rectangle([x + 6, 420, x + 36, 492], fill=(50, 54, 66))

    # door frame edge, as seen from a doorbell camera
    d.rectangle([0, 0, 56, h], fill=(70, 62, 58))
    d.rectangle([0, 0, w, 34], fill=(70, 62, 58))

    d.rectangle([0, h - 34, w, h], fill=(20, 20, 20))
    d.text((14, h - 26), "SIMULATED FRAME - local stand-in, not Ring footage", fill=(255, 214, 120))
    if caption:
        d.text((w - 14 - d.textlength(caption), h - 26), caption, fill=(230, 230, 230))

    buf = io.BytesIO()
    label = LABEL_MARKER + json.dumps({k: spec[k] for k in ("package", "person", "vehicle", "summary")})
    img.save(buf, format="JPEG", quality=82, comment=label.encode())
    return buf.getvalue()


def read_labels(image_bytes: bytes) -> dict[str, Any] | None:
    """Ground truth embedded by render_scene, or None for any other image."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        comment = img.info.get("comment")
    except Exception:
        return None
    if isinstance(comment, bytes):
        comment = comment.decode("utf-8", "ignore")
    if not comment or not comment.startswith(LABEL_MARKER):
        return None
    try:
        return json.loads(comment[len(LABEL_MARKER) :])
    except ValueError:
        return None


def _wall_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class StandInState:
    """What the stand-in camera has seen. Pass your own now_ms to control time in tests."""

    now_ms: Callable[[], int] = _wall_clock_ms
    signing_key: str = "sim-signing-key"
    webhook_url: str | None = None
    scene_changes: list[tuple[int, str]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    downloads: dict[str, bytes] = field(default_factory=dict)
    http: httpx.AsyncClient | None = None

    def scene_at(self, ts: int) -> str:
        current = "empty"
        for changed_at, scene in self.scene_changes:
            if changed_at <= ts:
                current = scene
            else:
                break
        return current

    def reset(self) -> None:
        self.scene_changes.clear()
        self.events.clear()
        self.downloads.clear()


def _require_bearer(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer ") or authorization.startswith("Bearer expired"):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _device_resource() -> dict[str, Any]:
    return {
        "type": "devices",
        "id": DEVICE_ID,
        "attributes": {"name": "Front Door (simulated)", "kind": "DoorbellPro"},
    }


def build_router(state: StandInState) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/devices")
    async def devices(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_bearer(authorization)
        return {"data": [_device_resource()]}

    @router.get("/v1/devices/{device_id}/status")
    async def status(device_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_bearer(authorization)
        return {"data": {"type": "device-status", "id": device_id, "attributes": {"online": True}}}

    @router.get("/v1/devices/{device_id}/capabilities")
    async def capabilities(device_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_bearer(authorization)
        return {
            "data": {
                "type": "device-capabilities",
                "id": device_id,
                "attributes": {"video": {"codecs": ["avc"]}, "motion_detection": {"supported": True}},
            }
        }

    @router.get("/v1/users/me")
    async def me(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_bearer(authorization)
        return {"data": {"type": "users", "id": ACCOUNT_ID, "attributes": {"first_name": "Simulated"}}}

    @router.get("/v1/history/devices/{device_id}/events")
    async def history(
        device_id: str,
        event_types: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_bearer(authorization)
        wanted = {t.split(".")[0] for t in event_types.split(",")} if event_types else None
        data = [e for e in reversed(state.events) if not wanted or e["attributes"]["event_type"] in wanted]
        return {"data": data[:50]}

    @router.post("/v1/devices/{device_id}/media/image/download")
    async def image_download(
        device_id: str, request: Request, authorization: str | None = Header(default=None)
    ) -> Response:
        _require_bearer(authorization)
        body = await request.json()
        now = state.now_ms()
        if body.get("type") == "at_timestamp":
            ts = int(body["timestamp"])
        elif body.get("type") == "latest_in_range":
            ts = min(int(body.get("end_timestamp") or now), now)
        else:
            return JSONResponse({"errors": [{"code": "INVALID_REQUEST", "detail": "unknown type"}]}, status_code=400)
        if ts > now + 1000:
            return JSONResponse(
                {"errors": [{"code": "INVALID_REQUEST", "detail": "timestamp is in the future"}]}, status_code=400
            )
        key = uuid.uuid4().hex
        state.downloads[key] = render_scene(state.scene_at(ts))
        location = str(request.url_for("standin_download")) + f"?security_token={key}&req_type=image_download"
        return Response(status_code=303, headers={"Location": location, "X-Request-ID": key})

    @router.get("/v1/download", name="standin_download")
    async def download(security_token: str, authorization: str | None = Header(default=None)) -> Response:
        if authorization:
            # The real pre-signed URL must be fetched without the bearer token.
            raise HTTPException(status_code=400, detail="Do not forward Authorization to the download URL")
        content = state.downloads.pop(security_token, None)
        if content is None:
            raise HTTPException(status_code=404, detail="expired")
        return Response(content=content, media_type="image/jpeg")

    @router.post("/v1/devices/{device_id}/media/video/download")
    async def video_download(device_id: str, authorization: str | None = Header(default=None)) -> Response:
        _require_bearer(authorization)
        return JSONResponse(
            {"errors": [{"code": "MEDIA_NOT_FOUND", "detail": "The stand-in records no video"}]}, status_code=416
        )

    @router.post("/v1/devices/{device_id}/media/streaming/whep/sessions")
    async def whep(device_id: str, authorization: str | None = Header(default=None)) -> Response:
        _require_bearer(authorization)
        return JSONResponse(
            {"errors": [{"code": "NOT_SUPPORTED", "detail": "The stand-in has no live view"}]}, status_code=501
        )

    @router.get("/_control/state")
    async def control_state() -> dict[str, Any]:
        now = state.now_ms()
        return {"scene": state.scene_at(now), "scenes": list(SCENES), "events": len(state.events), "now": now}

    @router.get("/_control/frame")
    async def control_frame() -> Response:
        return Response(content=render_scene(state.scene_at(state.now_ms())), media_type="image/jpeg")

    @router.post("/_control/scene")
    async def control_scene(request: Request) -> dict[str, Any]:
        body = await request.json()
        return await trigger(
            state,
            scene=body.get("scene", "empty"),
            event=body.get("event", "motion"),
            sub_type=body.get("sub_type"),
            send_webhook=bool(body.get("webhook", True)),
        )

    return router


async def trigger(
    state: StandInState,
    *,
    scene: str,
    event: str | None = "motion",
    sub_type: str | None = None,
    send_webhook: bool = True,
) -> dict[str, Any]:
    """Change what the simulated camera sees, and optionally emit a Ring-shaped event."""
    if scene not in SCENES:
        raise HTTPException(status_code=400, detail=f"unknown scene {scene!r}")
    now = state.now_ms()
    state.scene_changes.append((now, scene))
    result: dict[str, Any] = {"scene": scene, "ts": now, "event": None, "webhook": None}
    if not event:
        return result

    history_type = {"motion": "motion", "ding": "ding", "on_demand": "on_demand"}.get(event, "motion")
    event_id = f"standin-{uuid.uuid4().hex[:12]}"
    state.events.append(
        {
            "type": "history-events",
            "id": event_id,
            "attributes": {
                "event_type": history_type,
                "is_third_party_reviewed": False,
                "start": now,
                "end": now + 15_000,
            },
            "relationships": {"source": {"data": {"type": "devices", "id": DEVICE_ID}}},
        }
    )
    result["event"] = event_id

    if send_webhook and state.webhook_url and event in ("motion", "ding"):
        if event == "ding":
            hook_type, slug, attrs_extra = "button_press", "button_press", {}
        else:
            spec = SCENES[scene]
            sub = sub_type or ("human" if spec["person"] else "vehicle" if spec["vehicle"] else "motion")
            hook_type, slug, attrs_extra = "motion_detected", sub, {"sub_type": sub}
        payload = {
            "meta": {
                "version": "1.1",
                "time": "simulated",
                "request_id": str(uuid.uuid4()),
                "account_id": ACCOUNT_ID,
            },
            "data": {
                "id": f"{DEVICE_ID}_{slug}_{now}",
                "type": hook_type,
                "attributes": {"source": DEVICE_ID, "source_type": "devices", "timestamp": now, **attrs_extra},
                "relationships": {"devices": {"links": {"self": f"/v1/devices/{DEVICE_ID}"}}},
            },
        }
        raw = json.dumps(payload).encode()
        client = state.http or httpx.AsyncClient(timeout=10)
        try:
            resp = await client.post(
                state.webhook_url,
                content=raw,
                headers={"Content-Type": "application/json", "X-Signature": webhooks.sign(state.signing_key, raw)},
            )
            result["webhook"] = resp.status_code
        except httpx.HTTPError as exc:
            result["webhook"] = f"failed: {exc}"
        finally:
            if state.http is None:
                await client.aclose()
    return result


def build_app(state: StandInState | None = None):
    """A ready-to-serve FastAPI app. Serve it with uvicorn, or hand it to httpx.ASGITransport in tests."""
    from fastapi import FastAPI

    state = state or StandInState()
    app = FastAPI(title="Ring Partner API stand-in (not Ring)")
    app.state.standin = state
    app.include_router(build_router(state))
    return app
