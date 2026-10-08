"""The client against the local stand-in, in process (no network, no token)."""

from __future__ import annotations

import json

import httpx
import pytest

from ring_partner import MediaNotReady, RingAuthError, RingClient, RingError, parse, verify_signature
from ring_partner.cli import check
from ring_partner.standin import DEVICE_ID, StandInState, build_app, read_labels, trigger

BASE = "http://testserver"


class Now:
    def __init__(self) -> None:
        self.ms = 1_790_000_000_000

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
def now() -> Now:
    return Now()


@pytest.fixture
def state(now) -> StandInState:
    return StandInState(now_ms=now, signing_key="test-key")


@pytest.fixture
async def ring(state):
    client = RingClient(access_token="tok", api_base=BASE, transport=httpx.ASGITransport(app=build_app(state)))
    yield client
    await client.aclose()


async def test_lists_devices_and_reads_status(ring):
    devices = await ring.list_devices()
    assert [d.id for d in devices] == [DEVICE_ID] and devices[0].kind == "DoorbellPro"
    assert (await ring.device_status(DEVICE_ID))["online"] is True
    assert "video" in await ring.device_capabilities(DEVICE_ID)


async def test_event_history_is_newest_first_with_ms_timestamps(ring, state, now):
    await trigger(state, scene="vehicle", event="motion", send_webhook=False)
    now.ms += 60_000
    await trigger(state, scene="visitor", event="ding", send_webhook=False)
    events = await ring.event_history(DEVICE_ID)
    assert [e.event_type for e in events] == ["ding", "motion"]
    assert events[0].start_ms == now.ms and events[0].device_id == DEVICE_ID
    assert [e.event_type for e in await ring.event_history(DEVICE_ID, event_types="motion.vehicle")] == ["motion"]


async def test_snapshot_follows_the_303_without_forwarding_the_token(ring, state, now):
    await trigger(state, scene="package", event="motion", send_webhook=False)
    arrived = now.ms
    now.ms += 3_600_000
    await trigger(state, scene="empty", event=None)

    then = await ring.snapshot_at(DEVICE_ID, arrived)
    assert then.content_type == "image/jpeg" and read_labels(then.content)["package"] is True
    # The stand-in answers 400 if Authorization reaches the pre-signed URL, so this passing proves it did not.
    latest = await ring.latest_snapshot(DEVICE_ID, now.ms - 60_000, now.ms)
    assert read_labels(latest.content)["package"] is False


async def test_errors_are_typed(state):
    app = build_app(state)
    expired = RingClient(access_token="expired", api_base=BASE, transport=httpx.ASGITransport(app=app))
    with pytest.raises(RingAuthError) as info:
        await expired.list_devices()
    assert info.value.status == 401
    await expired.aclose()

    ring = RingClient(access_token="tok", api_base=BASE, transport=httpx.ASGITransport(app=app))
    with pytest.raises(MediaNotReady) as media:
        await ring.video_clip(DEVICE_ID, state.now_ms() - 10_000, 5_000)
    assert media.value.status == 416 and media.value.code == "MEDIA_NOT_FOUND"
    with pytest.raises(RingError):
        await ring.whep_stop("https://evil.example.com/session/1")
    await ring.aclose()

    with pytest.raises(RingAuthError):
        await RingClient(api_base=BASE, transport=httpx.ASGITransport(app=app)).list_devices()


async def test_refresh_token_mode_fetches_and_caches_an_access_token(state):
    app = build_app(state)
    calls = []

    @app.post("/oauth/token")
    async def token():
        calls.append(1)
        return {"access_token": "fresh", "expires_in": 3600, "refresh_token": "rotated"}

    ring = RingClient(
        refresh_token="r1",
        client_id="id",
        client_secret="secret",
        api_base=BASE,
        oauth_url=f"{BASE}/oauth/token",
        transport=httpx.ASGITransport(app=app),
    )
    await ring.list_devices()
    await ring.list_devices()
    assert len(calls) == 1 and ring.auth_mode == "refresh_token"
    await ring.aclose()


async def test_stand_in_sends_a_signed_v1_1_webhook(state):
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["raw"] = request.content
        received["signature"] = request.headers["X-Signature"]
        return httpx.Response(200)

    state.webhook_url = "https://partner.example.com/webhooks/ring"
    state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await trigger(state, scene="delivery", event="motion")
    await state.http.aclose()

    assert result["webhook"] == 200
    assert verify_signature("test-key", received["raw"], received["signature"])
    assert not verify_signature("test-key", json.dumps(json.loads(received["raw"]), indent=2).encode(), received["signature"])
    event = parse(received["raw"])
    assert (event.type, event.sub_type, event.device_id) == ("motion_detected", "human", DEVICE_ID)


async def test_cli_check_reports_each_capability(ring, state, capsys, tmp_path):
    await trigger(state, scene="package", event="motion", send_webhook=False)
    state.now_ms = lambda: __import__("time").time_ns() // 1_000_000  # the checker uses wall-clock time
    frame = tmp_path / "frame.jpg"
    code = await check(["--save-frame", str(frame)], client=ring)
    out = capsys.readouterr().out
    assert code == 0 and frame.read_bytes()[:2] == b"\xff\xd8"
    assert "ok    list devices" in out and "ok    event history" in out and "ok    latest snapshot" in out


async def test_403_for_an_unauthorized_time_range_is_no_media_not_a_bad_token():
    # The reply the Developer Playground gave on 8 Oct 2026 for a range with no events in it.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": [{"detail": "Requested time range is not within authorized boundaries"}]})

    ring = RingClient(access_token="tok", api_base=BASE, transport=httpx.MockTransport(handler))
    with pytest.raises(MediaNotReady) as info:
        await ring.latest_snapshot(DEVICE_ID, 1, 2)
    assert info.value.status == 403 and not isinstance(info.value, RingAuthError)
    await ring.aclose()
