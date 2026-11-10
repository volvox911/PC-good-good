"""Claude recognizer request shape, refusal handling, spend limits, missing credentials (17)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from gameoverlay.config import RecognitionSettings
from gameoverlay.demo.mock_vision import identification_for
from gameoverlay.demo.scene import DEMO_ITEMS, render_scene, to_jpeg
from gameoverlay.vision.recognizer import ClaudeRecognizer
from gameoverlay.vision.schema import GameIdentification
from gameoverlay.vision.usage import UsageTracker, estimate_cost


class FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.behaviour(kwargs)


class FakeClient:
    def __init__(self, behaviour):
        self.beta = SimpleNamespace(messages=FakeMessages(behaviour))


def ok_response(result: GameIdentification, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop,
        model="claude-opus-5-5",
        parsed_output=result,
        usage=SimpleNamespace(input_tokens=2000, output_tokens=300, cache_read_input_tokens=1500, cache_creation_input_tokens=0),
    )


@pytest.fixture
def settings():
    return RecognitionSettings()


def make(db, settings, behaviour, key="sk-ant-test-key"):
    usage = UsageTracker(db, "s1", lambda: settings)
    fake = FakeClient(behaviour)
    r = ClaudeRecognizer(lambda: key, lambda: settings, usage, client_factory=lambda k, c: fake)
    return r, fake


def frame():
    return to_jpeg(render_scene(DEMO_ITEMS[0]))


async def test_request_shape(db, settings):
    expected = identification_for(DEMO_ITEMS[0])
    r, fake = make(db, settings, lambda kw: ok_response(expected))
    out = await r.recognize([frame(), frame()])
    assert out.ok and out.result.title == expected.title
    kw = fake.beta.messages.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_format"] is GameIdentification
    assert kw["output_config"] == {"effort": "low"}
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in kw and "thinking" not in kw
    content = kw["messages"][0]["content"]
    imgs = [c for c in content if c["type"] == "image"]
    assert len(imgs) == 2 and imgs[0]["source"]["media_type"] == "image/jpeg"
    base64.standard_b64decode(imgs[0]["source"]["data"])
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "price" in kw["system"][0]["text"].lower()  # instructed NOT to reason about prices
    assert out.cost_usd == pytest.approx(estimate_cost("claude-opus-5-5", 2000, 300, 1500))


async def test_refusal_is_not_identified(db, settings):
    r, _ = make(db, settings, lambda kw: ok_response(None, stop="refusal"))
    out = await r.recognize([frame()])
    assert out.status == "refused" and out.result is None


async def test_truncated_output(db, settings):
    r, _ = make(db, settings, lambda kw: ok_response(None, stop="max_tokens"))
    assert (await r.recognize([frame()])).status == "invalid_output"


async def test_missing_api_key(db, settings):
    r, fake = make(db, settings, lambda kw: None, key=None)
    out = await r.recognize([frame()])
    assert out.status == "not_configured"
    assert fake.beta.messages.calls == []
    assert r.status()["state"] == "not_configured"


async def test_auth_error(db, settings):
    def boom(kw):
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=req), body=None)

    r, _ = make(db, settings, boom)
    out = await r.recognize([frame()])
    assert out.status == "auth_error"
    assert r.status()["state"] == "auth_failed"


async def test_network_error(db, settings):
    def boom(kw):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    r, _ = make(db, settings, boom)
    assert (await r.recognize([frame()])).status == "network"


async def test_spend_limit_blocks_calls(db, settings):
    settings.session_spend_limit_usd = 0.10
    expected = identification_for(DEMO_ITEMS[0])

    def expensive(kw):
        resp = ok_response(expected)
        resp.usage.input_tokens = 20000
        return resp

    r, fake = make(db, settings, expensive)
    first = await r.recognize([frame()])
    assert first.ok
    second = await r.recognize([frame()])
    assert second.status == "spend_limit"
    assert len(fake.beta.messages.calls) == 1
    assert r.usage.snapshot().limit_reached


async def test_requests_per_minute_cap(db, settings):
    settings.max_requests_per_minute = 2
    expected = identification_for(DEMO_ITEMS[0])
    r, fake = make(db, settings, lambda kw: ok_response(expected))
    assert (await r.recognize([frame()])).ok
    assert (await r.recognize([frame()])).ok
    assert (await r.recognize([frame()])).status == "request_cap"


async def test_untrusted_output_is_sanitized(db, settings):
    noisy = identification_for(DEMO_ITEMS[0], title="  Zelda\n\n  Ocarina   of Time  " + "x" * 500, upc_digits="0-45496-83043-4")
    r, _ = make(db, settings, lambda kw: ok_response(noisy))
    out = await r.recognize([frame()])
    assert "\n" not in out.result.title and len(out.result.title) <= 200
    assert out.result.upc_digits == "045496830434"
