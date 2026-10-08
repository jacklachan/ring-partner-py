"""Check a Ring token against the Ring Partner API, one capability at a time.

    ring-partner check --token "eyJ..."          # or set RING_ACCESS_TOKEN

Prints which calls work with this token and saves any frame it gets to
ring_check_frame.jpg. Useful right after pasting a new Developer Playground
token: it shows in a few seconds what that token can reach.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Awaitable

from .client import RING_API_BASE, MediaNotReady, RingClient, RingError


async def _step(label: str, coro: Awaitable[Any]) -> Any:
    try:
        result = await coro
        print(f"  ok    {label}")
        return result
    except MediaNotReady as exc:
        print(f"  none  {label}: {exc}")
    except RingError as exc:
        print(f"  FAIL  {label}: {exc}")
    except Exception as exc:  # network errors and the like
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")
    return None


async def check(argv: list[str] | None = None, *, client: RingClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ring-partner check", description="Check what a Ring token can reach.")
    parser.add_argument("--token", default=os.environ.get("RING_ACCESS_TOKEN"))
    parser.add_argument("--api-base", default=os.environ.get("RING_API_BASE", RING_API_BASE))
    parser.add_argument("--device-id")
    parser.add_argument("--save-frame", default="ring_check_frame.jpg", help="where to save a frame ('' to skip)")
    args = parser.parse_args(argv)
    if not args.token and client is None:
        parser.error("pass --token or set RING_ACCESS_TOKEN")

    ring = client or RingClient(access_token=args.token, api_base=args.api_base)
    print(f"Ring Partner API at {ring.api_base}")
    try:
        devices = await _step("list devices (GET /v1/devices)", ring.list_devices())
        if not devices:
            print("\nNo devices, so nothing else can be checked. Playground tokens last about 30 minutes.")
            return 1
        for d in devices:
            print(f"          {d.id}  {d.name}  ({d.kind})")
        device_id = args.device_id or devices[0].id

        status = await _step("device status", ring.device_status(device_id))
        if status is not None:
            print(f"          {status}")
        await _step("device capabilities", ring.device_capabilities(device_id))

        history = await _step("event history (GET /v1/history/devices/{id}/events)", ring.event_history(device_id))
        if history is not None:
            print(f"          {len(history)} event(s)")
            for event in history[:5]:
                print(f"          {event.event_type:<12} start={event.start_ms} id={event.id}")

        now = int(time.time() * 1000)
        frame = await _step(
            "latest snapshot in the last 10 minutes (image download, latest_in_range)",
            ring.latest_snapshot(device_id, now - 10 * 60_000, now),
        )
        if frame is None:
            frame = await _step(
                "latest snapshot in the last minute (image download, latest_in_range)",
                ring.latest_snapshot(device_id, now - 60_000, now),
            )
        if frame is None and not history:
            print("          No events yet: trigger Package, Vehicle or Motion in the Playground, then run this again.")
        if frame is None and history:
            frame = await _step(
                "snapshot at the newest event (image download, at_timestamp)",
                ring.snapshot_at(device_id, history[0].start_ms),
            )
        if frame is not None and args.save_frame:
            Path(args.save_frame).write_bytes(frame.content)
            print(f"          saved {args.save_frame} ({len(frame.content)} bytes, {frame.content_type})")
        print("  ....  live view (WHEP) needs a WebRTC peer; not checked here.")
    finally:
        if client is None:
            await ring.aclose()
    return 0


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["check"]:
        argv = argv[1:]
    elif argv and not argv[0].startswith("-"):
        sys.exit(f"unknown command {argv[0]!r}; try: ring-partner check --token ...")
    sys.exit(asyncio.run(check(argv)))


if __name__ == "__main__":
    main()
