# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Property-based test for :func:`kiro.backends.local.secret_provider.LocalSecretProvider.redact`.

Covers design correctness Property 3 (日志密钥脱敏 / log secret redaction) -
Requirements 6.5.

Generator design: to test "no secret plaintext remains AND non-secret content
is preserved" we build the text from interleaved *secret* and *filler* segments
drawn from **disjoint alphabets**:

- secret segments: lower-case letters + digits, length >= 4 (the provider only
  registers values whose length reaches the redaction minimum), and
- filler (non-secret) segments: upper-case letters, whitespace and punctuation.

Because no filler character is ever a secret-alphabet character, redaction
(which only ever replaces secret substrings) can never touch a filler segment.
This lets us assert preservation precisely while still exercising arbitrary
text shapes, secret counts, overlaps and adjacencies.
"""

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from kiro.backends.local.secret_provider import REDACTION_MASK, LocalSecretProvider

# Disjoint alphabets so a filler can never contain (a substring of) a secret.
_SECRET_ALPHABET = string.ascii_lowercase + string.digits
_FILLER_ALPHABET = string.ascii_uppercase + " \t.,:;!?-_/()[]{}"

# Secrets must reach the provider's redaction minimum length to be registered.
_secret = st.text(alphabet=_SECRET_ALPHABET, min_size=4, max_size=40)
_filler = st.text(alphabet=_FILLER_ALPHABET, min_size=0, max_size=20)


# Feature: aws-cloud-native-gateway, Property 3: 对任意文本及任意嵌入其中的密钥值，redact 输出不含任何密钥明文，同时保留非密钥内容
# **Validates: Requirements 6.5**
@settings(max_examples=200, deadline=None)
@given(
    segments=st.lists(st.tuples(_filler, _secret), min_size=0, max_size=8),
    trailing=_filler,
)
def test_redact_removes_secrets_and_preserves_other_text(segments, trailing):
    """For any text with any embedded (registered) secret values, ``redact``'s
    output contains none of the secret plaintext while preserving the
    non-secret content."""
    provider = LocalSecretProvider()

    # Build the text as: filler_0 secret_0 filler_1 secret_1 ... trailing_filler
    parts = []
    for filler, secret in segments:
        parts.append(filler)
        parts.append(secret)
    parts.append(trailing)
    text = "".join(parts)

    secrets = [secret for _, secret in segments]
    fillers = [filler for filler, _ in segments] + [trailing]

    # Register every secret value so redact() knows to mask it.
    for secret in secrets:
        provider.register_secret(secret)

    redacted = provider.redact(text)

    # (a) No secret plaintext survives in the redacted output.
    for secret in secrets:
        assert secret not in redacted

    # (b) Non-secret content is preserved: filler segments use a disjoint
    #     alphabet, so redaction never alters them and each remains intact.
    for filler in fillers:
        if filler:
            assert filler in redacted

    # When at least one secret was present, the mask must appear.
    if secrets:
        assert REDACTION_MASK in redacted


# ==================================================================================================
# Complementary example-based unit tests
# ==================================================================================================


def test_redact_masks_registered_secret_and_keeps_surrounding_text():
    provider = LocalSecretProvider()
    provider.register_secret("topsecretvalue")
    redacted = provider.redact("Authorization: Bearer topsecretvalue end")
    assert "topsecretvalue" not in redacted
    assert REDACTION_MASK in redacted
    assert redacted.startswith("Authorization: Bearer ")
    assert redacted.endswith(" end")


def test_redact_ignores_short_values():
    provider = LocalSecretProvider()
    provider.register_secret("ab")  # below the redaction minimum length
    assert provider.redact("ab cd") == "ab cd"


def test_redact_empty_text_is_noop():
    provider = LocalSecretProvider()
    assert provider.redact("") == ""


def test_redact_longer_secret_containing_shorter_is_fully_masked():
    provider = LocalSecretProvider()
    provider.register_secret("abcdef")
    provider.register_secret("abcdef123456")
    redacted = provider.redact("value=abcdef123456")
    assert "abcdef123456" not in redacted
    assert redacted == f"value={REDACTION_MASK}"
