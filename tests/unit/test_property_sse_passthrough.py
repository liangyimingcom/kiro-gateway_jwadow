# -*- coding: utf-8 -*-

"""
Property-based test for design.md Property 12
(SSE 流式透传不变量 / SSE streaming pass-through invariants).

This drives the *real* route-level forwarding generator used by the OpenAI
endpoint - :func:`kiro.streaming_openai.stream_kiro_to_openai` (the generator the
route wraps in ``StreamingResponse(..., media_type="text/event-stream")``) - with
an injected mock upstream stream and asserts the streaming pass-through
invariants:

  A. every forwarded frame is a well-formed SSE frame (``data: ...\\n\\n``);
  B. the forwarded content frames match the upstream content chunks in *content*
     and *order* exactly;
  C. forwarding begins on the *first* upstream chunk - the first content frame is
     emitted after exactly one upstream chunk has been pulled, proving the gateway
     does not buffer the full response before forwarding;
  D. the stream ends with the same terminator the gateway always emits
     (``data: [DONE]\\n\\n``), exactly once.

The upstream is injected via a minimal async fake of ``httpx.Response`` whose
``aiter_bytes()`` yields Kiro AWS-event-stream content chunks built from arbitrary
text (``{"content": "..."}``), followed by a usage terminator so the gateway takes
its normal (non-truncation) completion path. The real ``AwsEventStreamParser`` and
OpenAI frame formatting run unmocked.

Note: the gateway intentionally de-duplicates *consecutive identical* upstream
content chunks (``AwsEventStreamParser`` content dedup). To isolate the transport
invariant (order / first-chunk / terminator) from that content-level optimisation,
the generated upstream sequence collapses consecutive duplicates; every other
sequence shape (including non-consecutive repeats, unicode, braces, quotes) is
exercised. The fake reasoning parser is disabled so content passes through 1:1.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings, strategies as st

from kiro.streaming_openai import stream_kiro_to_openai

_DONE_FRAME = "data: [DONE]\n\n"


# ==================================================================================================
# Upstream encoding + injected fake response
# ==================================================================================================


def _content_chunk(text: str) -> bytes:
    """Encode one upstream Kiro content event (``{"content": "<text>"}``)."""
    return json.dumps({"content": text}).encode("utf-8")


def _usage_chunk(value) -> bytes:
    """Encode a trailing usage event so the stream completes normally (no truncation path)."""
    return json.dumps({"usage": value}).encode("utf-8")


class _FakeUpstreamResponse:
    """
    Minimal async stand-in for ``httpx.Response`` exposing only what the real
    streaming code touches: ``aiter_bytes()`` and ``aclose()``. ``pulled`` records
    how many upstream chunks have been consumed so the test can prove forwarding
    starts on the first chunk (no full-response buffering).
    """

    def __init__(self, byte_chunks):
        self._chunks = list(byte_chunks)
        self.pulled = 0
        self.closed = False
        self.status_code = 200

    async def aiter_bytes(self):
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk

    async def aclose(self):
        self.closed = True


# ==================================================================================================
# Frame parsing helpers
# ==================================================================================================


def _parse_frame(frame: str):
    """Return the decoded JSON payload of an SSE data frame, or ``None`` for ``[DONE]``."""
    body = frame[len("data: "):].strip()
    if body == "[DONE]":
        return None
    return json.loads(body)


def _frame_content(frame: str):
    """Return the ``delta.content`` string of a content frame, else ``None``."""
    payload = _parse_frame(frame)
    if payload is None:
        return None
    delta = payload.get("choices", [{}])[0].get("delta", {})
    content = delta.get("content")
    return content if isinstance(content, str) else None


# ==================================================================================================
# Strategy: arbitrary upstream content-chunk sequences (no consecutive duplicates)
# ==================================================================================================

_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=1,
    max_size=24,
)


def _drop_consecutive_dups(items):
    out = []
    for item in items:
        if not out or out[-1] != item:
            out.append(item)
    return out


@st.composite
def _upstream_content_sequences(draw):
    raw = draw(st.lists(_TEXT, min_size=1, max_size=15))
    return _drop_consecutive_dups(raw)


# ==================================================================================================
# Property test
# ==================================================================================================


# Feature: aws-cloud-native-gateway, Property 12: 对任意上游数据块序列，网关转发的 SSE 帧序列在内容与顺序上与上游一致，收到首块即转发，并以一致的结束标记（如 [DONE]）收尾
# Validates: Requirements 10.2, 11.2
@given(_upstream_content_sequences())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_property_12_sse_passthrough_invariants(content_chunks):
    """
    For any upstream chunk sequence, the gateway's forwarded SSE frame sequence
    matches the upstream chunks in content and order, forwarding begins on the
    first chunk (no full-response buffering), and the stream ends with the same
    ``[DONE]`` terminator.

    Validates: Requirements 10.2, 11.2
    """
    byte_chunks = [_content_chunk(c) for c in content_chunks] + [_usage_chunk(1)]
    response = _FakeUpstreamResponse(byte_chunks)

    model_cache = MagicMock()
    model_cache.get_max_input_tokens.return_value = 200000
    auth_manager = MagicMock()
    client = MagicMock()

    frames = []
    pulled_at_first_content = {"value": None}

    async def _consume():
        async for frame in stream_kiro_to_openai(
            client, response, "test-model", model_cache, auth_manager
        ):
            frames.append(frame)
            if pulled_at_first_content["value"] is None and _frame_content(frame) is not None:
                pulled_at_first_content["value"] = response.pulled

    # Disable the fake-reasoning thinking parser so content is forwarded verbatim.
    with patch("kiro.streaming_core.FAKE_REASONING_ENABLED", False):
        asyncio.run(_consume())

    # Invariant A: every frame is a well-formed SSE data frame.
    assert frames, "gateway produced no frames"
    for frame in frames:
        assert frame.startswith("data: "), f"frame missing SSE prefix: {frame!r}"
        assert frame.endswith("\n\n"), f"frame missing SSE terminator: {frame!r}"

    # Invariant B: forwarded content matches upstream content in content AND order.
    forwarded_content = [c for c in (_frame_content(f) for f in frames) if c is not None]
    assert forwarded_content == content_chunks

    # Invariant C: forwarding begins on the first upstream chunk (no full buffering).
    assert pulled_at_first_content["value"] == 1, (
        "gateway did not forward on the first upstream chunk; "
        f"pulled={pulled_at_first_content['value']}"
    )
    assert pulled_at_first_content["value"] < len(byte_chunks)

    # Invariant D: the stream ends with exactly one [DONE] terminator.
    assert frames[-1] == _DONE_FRAME
    assert sum(1 for f in frames if f == _DONE_FRAME) == 1
