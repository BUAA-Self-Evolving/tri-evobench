from __future__ import annotations

import json

from trievo_bench.model import parse_reply


def test_parse_reply_ignores_malformed_state_update() -> None:
    reply = parse_reply(json.dumps({
        "response": "usable answer",
        "actions": [],
        "state_update": ["not", "a", "mapping"],
    }))

    assert reply.text == "usable answer"
    assert reply.state_update == {}
