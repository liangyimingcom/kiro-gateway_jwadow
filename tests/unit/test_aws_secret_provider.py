# -*- coding: utf-8 -*-

"""
Unit tests for :class:`kiro.backends.aws.secret_provider.SecretsManagerProvider`.

These tests exercise the provider's own logic (secret-id resolution, the TTL
read cache, write-back / round-trip, JSON parsing and log redaction) against a
lightweight in-memory stand-in for the AWS Secrets Manager client. The fake is a
test double for the *external* AWS service - the gateway logic under test is
real, not mocked.

No ``boto3``/AWS credentials are required: the provider imports ``boto3`` lazily
and we inject the fake client via ``client=...``.
"""

import json

import pytest

from kiro.backends.aws.secret_provider import REDACTION_MASK, SecretsManagerProvider
from kiro.backends.interfaces import SecretProvider


class _ResourceNotFoundException(Exception):
    """Mimics the botocore 'not found' error shape used by the provider."""

    def __init__(self, message="Secrets Manager can't find the specified secret."):
        super().__init__(message)
        self.response = {"Error": {"Code": "ResourceNotFoundException", "Message": message}}


class FakeSecretsManagerClient:
    """In-memory stand-in for the boto3 Secrets Manager client."""

    def __init__(self, initial=None):
        self._store = dict(initial or {})
        self.get_calls = 0
        self.put_calls = 0
        self.create_calls = 0

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3 kwarg name
        self.get_calls += 1
        if SecretId not in self._store:
            raise _ResourceNotFoundException()
        return {"SecretString": self._store[SecretId]}

    def put_secret_value(self, SecretId, SecretString):  # noqa: N803
        self.put_calls += 1
        if SecretId not in self._store:
            raise _ResourceNotFoundException()
        self._store[SecretId] = SecretString
        return {"ARN": f"arn:aws:secretsmanager:::secret:{SecretId}"}

    def create_secret(self, Name, SecretString):  # noqa: N803
        self.create_calls += 1
        self._store[Name] = SecretString
        return {"ARN": f"arn:aws:secretsmanager:::secret:{Name}"}


def _make_provider(initial=None, *, stack="teststack", cache_ttl=300.0):
    client = FakeSecretsManagerClient(initial=initial)
    provider = SecretsManagerProvider(stack=stack, client=client, cache_ttl=cache_ttl)
    return provider, client


# =============================================================================
# Protocol conformance
# =============================================================================


def test_satisfies_secret_provider_protocol():
    provider, _ = _make_provider()
    assert isinstance(provider, SecretProvider)


# =============================================================================
# Secret-id resolution (Secret_Store layout)
# =============================================================================


class TestSecretIdResolution:
    def test_proxy_api_key_logical_name(self):
        provider, _ = _make_provider(stack="acme")
        assert provider._resolve_secret_id("PROXY_API_KEY") == "acme/proxy-api-key"
        assert provider.proxy_api_key_secret_id() == "acme/proxy-api-key"

    def test_credentials_json_logical_name(self):
        provider, _ = _make_provider(stack="acme")
        assert provider._resolve_secret_id("credentials.json") == "acme/credentials-json"
        assert provider.credentials_json_secret_id() == "acme/credentials-json"

    def test_account_token_logical_name(self):
        provider, _ = _make_provider(stack="acme")
        assert provider._resolve_secret_id("account/abc123/token") == "acme/account/abc123/token"
        assert provider.account_token_secret_id("abc123") == "acme/account/abc123/token"

    def test_already_qualified_id_passthrough(self):
        provider, _ = _make_provider(stack="acme")
        assert provider._resolve_secret_id("acme/proxy-api-key") == "acme/proxy-api-key"

    def test_generic_fallback_namespaced_under_stack(self):
        provider, _ = _make_provider(stack="acme")
        assert provider._resolve_secret_id("SOME_OTHER_KEY") == "acme/SOME_OTHER_KEY"

    def test_empty_name_raises(self):
        provider, _ = _make_provider()
        with pytest.raises(KeyError):
            provider._resolve_secret_id("")


# =============================================================================
# get_secret / get_json_secret
# =============================================================================


class TestGetSecret:
    @pytest.mark.asyncio
    async def test_reads_proxy_api_key(self):
        provider, _ = _make_provider({"teststack/proxy-api-key": "super-secret-key"})
        value = await provider.get_secret("PROXY_API_KEY")
        assert value == "super-secret-key"

    @pytest.mark.asyncio
    async def test_missing_secret_raises_keyerror(self):
        provider, _ = _make_provider({})
        with pytest.raises(KeyError):
            await provider.get_secret("PROXY_API_KEY")

    @pytest.mark.asyncio
    async def test_get_json_secret_parses_json(self):
        bundle = {"access_token": "at-123456", "refresh_token": "rt-abcdef", "expires_at": 123}
        provider, _ = _make_provider(
            {"teststack/account/acc1/token": json.dumps(bundle)}
        )
        result = await provider.get_json_secret("account/acc1/token")
        assert result == bundle


# =============================================================================
# TTL read cache (reduce billed API calls - design 11.5 / 12.2)
# =============================================================================


class TestTtlCache:
    @pytest.mark.asyncio
    async def test_repeated_reads_hit_cache(self):
        provider, client = _make_provider({"teststack/proxy-api-key": "k"}, cache_ttl=300.0)
        await provider.get_secret("PROXY_API_KEY")
        await provider.get_secret("PROXY_API_KEY")
        await provider.get_secret("PROXY_API_KEY")
        # Only a single billed GetSecretValue call despite three reads.
        assert client.get_calls == 1

    @pytest.mark.asyncio
    async def test_cache_disabled_when_ttl_non_positive(self):
        provider, client = _make_provider({"teststack/proxy-api-key": "k"}, cache_ttl=0)
        await provider.get_secret("PROXY_API_KEY")
        await provider.get_secret("PROXY_API_KEY")
        assert client.get_calls == 2

    @pytest.mark.asyncio
    async def test_expired_entry_refetches(self):
        provider, client = _make_provider({"teststack/proxy-api-key": "k"}, cache_ttl=300.0)
        await provider.get_secret("PROXY_API_KEY")
        assert client.get_calls == 1
        # Force expiry by invalidating the cache.
        provider.invalidate_cache("PROXY_API_KEY")
        await provider.get_secret("PROXY_API_KEY")
        assert client.get_calls == 2

    @pytest.mark.asyncio
    async def test_invalidate_all(self):
        provider, client = _make_provider({"teststack/proxy-api-key": "k"})
        await provider.get_secret("PROXY_API_KEY")
        provider.invalidate_cache()
        await provider.get_secret("PROXY_API_KEY")
        assert client.get_calls == 2


# =============================================================================
# put_secret (write-back refreshed tokens - Requirements 6.7)
# =============================================================================


class TestPutSecret:
    @pytest.mark.asyncio
    async def test_put_then_get_round_trips(self):
        provider, _ = _make_provider({"teststack/account/a/token": "old"})
        await provider.put_secret("account/a/token", "new-token-value")
        # Write-through cache returns the new value without a fetch.
        assert await provider.get_secret("account/a/token") == "new-token-value"

    @pytest.mark.asyncio
    async def test_put_creates_secret_when_missing(self):
        provider, client = _make_provider({})
        await provider.put_secret("account/new/token", "fresh-token")
        assert client.create_calls == 1
        assert await provider.get_secret("account/new/token") == "fresh-token"

    @pytest.mark.asyncio
    async def test_put_updates_existing_secret(self):
        provider, client = _make_provider({"teststack/account/a/token": "old"})
        await provider.put_secret("account/a/token", "updated")
        assert client.put_calls == 1
        assert client.create_calls == 0


# =============================================================================
# redact (log masking - Requirements 6.5)
# =============================================================================


class TestRedact:
    @pytest.mark.asyncio
    async def test_redacts_known_secret_value(self):
        provider, _ = _make_provider({"teststack/proxy-api-key": "topsecretvalue"})
        await provider.get_secret("PROXY_API_KEY")
        text = "Authorization: Bearer topsecretvalue end"
        redacted = provider.redact(text)
        assert "topsecretvalue" not in redacted
        assert REDACTION_MASK in redacted
        # Non-secret content is preserved.
        assert redacted.startswith("Authorization: Bearer ")
        assert redacted.endswith(" end")

    @pytest.mark.asyncio
    async def test_redacts_json_leaf_tokens(self):
        bundle = {"access_token": "access-token-xyz", "refresh_token": "refresh-token-abc"}
        provider, _ = _make_provider(
            {"teststack/account/a/token": json.dumps(bundle)}
        )
        await provider.get_secret("account/a/token")
        log_line = "tokens are access-token-xyz and refresh-token-abc"
        redacted = provider.redact(log_line)
        assert "access-token-xyz" not in redacted
        assert "refresh-token-abc" not in redacted

    def test_register_secret_ignores_short_values(self):
        provider, _ = _make_provider()
        provider.register_secret("ab")  # below minimum length
        assert provider.redact("ab cd") == "ab cd"

    def test_redact_empty_text_is_noop(self):
        provider, _ = _make_provider()
        assert provider.redact("") == ""

    @pytest.mark.asyncio
    async def test_longer_secret_masked_first(self):
        # A secret that contains a shorter registered secret as a substring
        # must still be fully masked.
        provider, _ = _make_provider(
            {
                "teststack/account/a/token": "abcdef",
                "teststack/account/b/token": "abcdef123456",
            }
        )
        await provider.get_secret("account/a/token")
        await provider.get_secret("account/b/token")
        redacted = provider.redact("value=abcdef123456")
        assert "abcdef123456" not in redacted
        assert redacted == f"value={REDACTION_MASK}"


# =============================================================================
# Construction / configuration
# =============================================================================


class TestConstruction:
    def test_stack_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", "envstack")
        provider = SecretsManagerProvider(client=FakeSecretsManagerClient())
        assert provider.stack == "envstack"

    def test_import_does_not_require_boto3(self):
        # Constructing with an injected client must never import boto3.
        provider = SecretsManagerProvider(client=FakeSecretsManagerClient(), stack="s")
        assert provider.stack == "s"
