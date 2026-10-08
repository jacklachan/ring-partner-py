# ring-partner

An unofficial async Python client for the [Ring Partner API](https://developer.amazon.com/docs/ring/api-documentation.html),
with webhook verification and a local stand-in you can test against.

Ring's docs say "There is no official Ring Partner SDK", so every app starts by re-writing the same
few hundred lines: auth, JSON:API unwrapping, the two-step media download, webhook signatures. This
package is those lines, typed and tested, so you can start on your app instead.

> Not affiliated with, endorsed by, or supported by Ring or Amazon. "Ring" is a trademark of its owner.

## Install

```bash
pip install "git+https://github.com/jacklachan/ring-partner-py"
```

Python 3.10+. The only runtime dependency is `httpx`.

## Use

```python
import asyncio, time
from ring_partner import RingClient

async def main():
    # A token from the Ring Developer Playground (lasts about 30 minutes)
    ring = RingClient(access_token="eyJ...")
    try:
        device = (await ring.list_devices())[0]
        for event in await ring.event_history(device.id, event_types="motion.human,ding"):
            print(event.event_type, event.start_ms)

        now = int(time.time() * 1000)
        frame = await ring.latest_snapshot(device.id, now - 10 * 60_000, now)
        open("porch.jpg", "wb").write(frame.content)
    finally:
        await ring.aclose()

asyncio.run(main())
```

For a registered app, use the refresh-token mode. The access token is fetched and renewed for you:

```python
ring = RingClient(refresh_token="...", client_id="...", client_secret="...")
```

### What it covers

| Method | Ring endpoint |
|---|---|
| `list_devices()` | `GET /v1/devices` |
| `device_status(id)`, `device_capabilities(id)` | `GET /v1/devices/{id}/status`, `/capabilities` |
| `user_profile()` | `GET /v1/users/me` |
| `event_history(id, event_types=..., max_pages=...)` | `GET /v1/history/devices/{id}/events`, following `links.next` |
| `snapshot_at(id, timestamp_ms)` | `POST /v1/devices/{id}/media/image/download` (`at_timestamp`) |
| `latest_snapshot(id, start_ms, end_ms)` | same, `latest_in_range` |
| `video_clip(id, timestamp_ms, duration_ms, retry_until=...)` | `POST /v1/devices/{id}/media/video/download` |
| `whep_start(id, sdp_offer)`, `whep_stop(session_url)` | `POST /v1/devices/{id}/media/streaming/whep/sessions` |
| `set_access_token(token)` | swap in a fresh Playground token without rebuilding the client |

Details that are easy to get wrong, handled for you:

- **Media downloads answer `303` with a pre-signed URL.** The client fetches that URL by hand and does
  not send your bearer token to it.
- **`416 MEDIA_NOT_FOUND`, `425 RECORDING_NOT_READY`, and the `403` Ring returns for a time range your app
  is not authorized for** raise `MediaNotReady`, so "no media" is never mistaken for an expired token. `video_clip(retry_until=...)`
  retries the unchanged request with backoff and jitter, as Ring's guidance describes.
- **Expired tokens** raise `RingAuthError` (`.status` is 401 or 403), separate from other `RingError`s.
- **Multi-camera devices:** pass `component_id=` to the media calls.
- **History timestamps** are returned as integers in epoch milliseconds, whether Ring sends numbers or numeric strings.
- **`whep_stop`** refuses to send your token to a session URL that is not on Ring's API host.

## Webhooks

Ring signs each webhook with HMAC-SHA256 over the **raw** body and sends `X-Signature: sha256=<hex>`.
Verify first, parse second: re-serialising the JSON changes the bytes and breaks the signature.

```python
from fastapi import FastAPI, Request, Response
from ring_partner import parse, verify_signature, WebhookError

app = FastAPI()
seen: set[str] = set()

@app.post("/webhooks/ring")
async def ring_webhook(request: Request):
    raw = await request.body()
    if not verify_signature(SIGNING_KEY, raw, request.headers.get("x-signature")):
        return Response(status_code=401)
    try:
        event = parse(raw)
    except WebhookError:
        return Response(status_code=400)
    if event.request_id in seen:          # Ring retries; dedupe on meta.request_id
        return {"status": "duplicate"}
    seen.add(event.request_id)
    if event.type == "motion_detected":
        print(event.device_id, event.sub_type, event.timestamp_ms)
    return {"status": "ok"}               # answer within 5 seconds; do slow work afterwards
```

`sign(key, raw)` produces the header Ring would send, for your own tests.

## Check a token from the command line

```bash
ring-partner check --token "eyJ..."
```

```
Ring Partner API at https://api.amazonvision.com
  ok    list devices (GET /v1/devices)
  ok    device status
  ok    device capabilities
  ok    event history (GET /v1/history/devices/{id}/events)
  none  latest snapshot in the last 10 minutes (image download, latest_in_range): Ring API 416: ...
```

Example output. `ok` worked, `none` means Ring had no media for that request, `FAIL` is an error. Any frame is saved to
`ring_check_frame.jpg`.

## Test without Ring: the stand-in

`ring_partner.standin` is a small FastAPI app that answers like the Ring Partner API for one doorbell:
devices, status, event history, the 303 image download, and signed v1.1 webhooks. **It is not Ring and
its frames are not Ring footage**; they are simple drawings, watermarked as simulated, with ground-truth
labels embedded so tests can assert on what a frame shows.

```bash
pip install "ring-partner[standin] @ git+https://github.com/jacklachan/ring-partner-py"
```

```python
import httpx
from ring_partner import RingClient
from ring_partner.standin import DEVICE_ID, StandInState, build_app, read_labels, trigger

async def test_my_app_sees_the_package():
    state = StandInState()
    ring = RingClient(access_token="t", api_base="http://testserver",
                      transport=httpx.ASGITransport(app=build_app(state)))
    await trigger(state, scene="package", event="motion", send_webhook=False)

    event = (await ring.event_history(DEVICE_ID))[0]
    frame = await ring.snapshot_at(DEVICE_ID, event.start_ms)
    assert read_labels(frame.content)["package"] is True
```

Scenes: `empty`, `package`, `delivery`, `vehicle`, `visitor`, `pickup`. Set `state.webhook_url` and
`trigger(...)` also POSTs a signed webhook to your handler. Pass `StandInState(now_ms=...)` to control time.

The stand-in follows the published documentation. It has no video clips or live view (those return
416 and 501).

## Status

- 15 tests, run in CI on Python 3.10 and 3.13, all against the stand-in.
- Written from Ring's public documentation and hello-world sample. Checked against the live API with a
  Developer Playground token on 8 October 2026: device discovery, status, capabilities and Event History
  worked, as did `snapshot_at` for a Playground event and `whep_start` from a browser offer. Image download
  answered `403 Requested time range is not within authorized boundaries` for a window with no events in
  it, which the client raises as `MediaNotReady`. Video clips, webhooks and refresh-token sign-in have
  **not** been confirmed live yet. If a call behaves
  differently for you, please open an issue with the status code and error body.
- Not covered yet: account linking (nonce verification), device configurations and location, RTSP,
  chime audio playback, sensor events.

## Where it came from

Extracted from [Porchlight](https://github.com/jacklachan/Porchlight), a caretaking app built on the Ring
Partner API for the Build, Ship, Shape: Amazon Developer Hackathon.

## License

MIT
