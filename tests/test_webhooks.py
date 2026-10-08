import json

import pytest

from ring_partner import WebhookError, parse, sign, verify_signature

MOTION = {
    "meta": {"version": "1.1", "time": "2026-02-13T13:39:57Z", "request_id": "r-1", "account_id": "ava1.ring.account.X"},
    "data": {
        "id": "dev_motion_1786715596787",
        "type": "motion_detected",
        "attributes": {
            "source": "dev",
            "source_type": "devices",
            "timestamp": 1786715596787,
            "sub_type": "human",
            "component_ids": ["0"],
        },
    },
}


def test_parse_reads_the_documented_motion_payload():
    event = parse(json.dumps(MOTION).encode())
    assert (event.type, event.sub_type, event.device_id) == ("motion_detected", "human", "dev")
    assert event.request_id == "r-1" and event.account_id == "ava1.ring.account.X"
    assert event.timestamp_ms == 1786715596787 and event.component_ids == ["0"]
    assert event.is_device_event


def test_subscription_webhook_is_not_a_device_event():
    payload = {
        "meta": {"version": "1.1", "time": "t", "request_id": "r-2", "account_id": "a"},
        "data": {"id": "e", "type": "subscription_activated", "attributes": {"source": "subscriptions"}},
    }
    event = parse(json.dumps(payload).encode())
    assert not event.is_device_event and event.device_id is None


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"meta": {}, "data": {}}'])
def test_parse_rejects_other_shapes(body):
    with pytest.raises(WebhookError):
        parse(body)


def test_signature_is_a_hex_digest_over_the_raw_body():
    raw = b'{"a":1}'
    header = sign("key", raw)
    assert header.startswith("sha256=") and len(header) == 7 + 64
    assert verify_signature("key", raw, header)
    assert verify_signature("key", raw, header.removeprefix("sha256="))
    assert not verify_signature("key", raw + b" ", header)
    assert not verify_signature("other", raw, header)
    assert not verify_signature("key", raw, None)
