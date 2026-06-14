# -*- coding: utf-8 -*-

"""
Property-based test for Task 4.2 / design Property 11 (模型解析一致性).

The AWS cloud-native refactor keeps a SINGLE ``ModelResolver`` implementation
(``kiro/model_resolver.py``) and only re-wires how ``kiro.config`` sources its
values. Because there is no second implementation to diff against, this test
encodes the *resolution invariants* that must hold for ANY model name or alias
input - including hidden models and aliases such as ``auto-kiro`` - so that any
future regression in the resolver (or in how config feeds it) is caught:

  * Determinism: two independently constructed resolvers with identical config,
    and repeated calls on the same resolver, return byte-for-byte identical
    ``ModelResolution`` objects for the same input.
  * Pipeline fidelity: ``normalized`` always equals
    ``normalize_model_name(alias-resolved input)`` and ``is_verified`` is true
    iff the model was found in the cache or hidden-model layers.
  * Alias transparency: an alias resolves exactly like its target model id.
  * Hidden models still resolve (to their internal Kiro id, verified).
  * Known cache models resolve consistently to themselves (verified).
  * Unknown inputs are handled consistently via deterministic pass-through.

Uses Hypothesis with max_examples >= 100.
"""

from hypothesis import given, settings, strategies as st

from kiro.cache import ModelInfoCache
from kiro.model_resolver import (
    ModelResolver,
    ModelResolution,
    normalize_model_name,
    to_runtime_model_id,
)


# --- Fixed resolver configuration (shared by every constructed resolver) -----
# Cache ids are already in normalized (dot) form so ``normalize_model_name`` is
# an identity on them.
CACHE_MODELS = [
    "auto",
    "claude-sonnet-4",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "claude-opus-4.5",
]

# Hidden models are NOT present in the cache and normalize to themselves, so the
# hidden-model layer (layer 3) is actually reachable for them.
HIDDEN_MODELS = {
    "claude-3.7-sonnet": "CLAUDE_3_7_SONNET_20250219_V1_0",
    "claude-legacy-x": "LEGACY_X_INTERNAL_ID",
}

# Aliases include the real default (auto-kiro -> auto) plus an alias that maps to
# a cache model and one that maps to an unknown (pass-through) model. None of the
# alias *targets* are themselves alias keys.
ALIASES = {
    "auto-kiro": "auto",            # alias -> cache hit
    "my-opus": "claude-opus-4.5",   # alias -> cache hit
    "ghost": "claude-ghost-9",      # alias -> pass-through (unknown)
}

HIDDEN_FROM_LIST = ["auto"]


def _make_resolver() -> ModelResolver:
    """Build a fresh ModelResolver with the fixed configuration above."""
    cache = ModelInfoCache()
    # Populate directly (synchronous) - mirrors the existing resolver unit tests.
    cache._cache = {mid: {"modelId": mid, "modelName": mid} for mid in CACHE_MODELS}
    return ModelResolver(
        cache=cache,
        hidden_models=dict(HIDDEN_MODELS),
        aliases=dict(ALIASES),
        hidden_from_list=list(HIDDEN_FROM_LIST),
    )


# Curated inputs that exercise every resolution layer plus normalization quirks.
_INTERESTING_INPUTS = [
    # aliases
    "auto-kiro", "my-opus", "ghost",
    # alias targets
    "auto", "claude-opus-4.5", "claude-ghost-9",
    # hidden models (and a client-format variant that normalizes to a hidden key)
    "claude-3.7-sonnet", "claude-3-7-sonnet", "claude-legacy-x",
    # cache models in various client formats
    "claude-sonnet-4", "claude-sonnet-4-5", "claude-sonnet-4.5",
    "claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-haiku-4-5-latest",
    "claude-opus-4-5", "claude-4.5-opus-high",
    # unknown / pass-through
    "gpt-4", "deepseek-3.2", "totally-unknown-model",
    # edge cases
    "", "claude-sonnet-4.5[1m]", "AUTO", "Claude-Sonnet-4-5",
]


@st.composite
def _model_like_names(draw):
    """Generate structured Claude-style model names across known client formats."""
    family = draw(st.sampled_from(["haiku", "sonnet", "opus"]))
    major = draw(st.integers(min_value=1, max_value=9))
    minor = draw(st.integers(min_value=0, max_value=9))
    style = draw(
        st.sampled_from(["dash", "dot", "major_only", "date", "latest", "ctx", "inverted"])
    )
    if style == "dash":
        return f"claude-{family}-{major}-{minor}"
    if style == "dot":
        return f"claude-{family}-{major}.{minor}"
    if style == "major_only":
        return f"claude-{family}-{major}"
    if style == "date":
        return f"claude-{family}-{major}-{minor}-20250514"
    if style == "latest":
        return f"claude-{family}-{major}-{minor}-latest"
    if style == "ctx":
        return f"claude-{family}-{major}.{minor}[1m]"
    # inverted
    return f"claude-{major}.{minor}-{family}-high"


_INPUT_STRATEGY = st.one_of(
    st.sampled_from(_INTERESTING_INPUTS),
    _model_like_names(),
    st.text(max_size=40),
)


# Feature: aws-cloud-native-gateway, Property 11: 对任意模型名称或别名输入（含隐藏模型与 auto-kiro 等别名），改造后 ModelResolver 解析结果与改造前一致
# Validates Requirements 10.5
@settings(max_examples=300, deadline=None)
@given(external_model=_INPUT_STRATEGY)
def test_property_11_model_resolution_consistency(external_model):
    resolver_a = _make_resolver()
    resolver_b = _make_resolver()

    res = resolver_a.resolve(external_model)

    # --- Structural invariants (hold for every input) ------------------------
    assert isinstance(res, ModelResolution)
    assert res.original_request == external_model
    assert res.source in {"cache", "hidden", "passthrough"}
    assert res.is_verified == (res.source in {"cache", "hidden"})

    # normalized must equal the documented pipeline: alias-resolve then normalize.
    alias_resolved = ALIASES.get(external_model, external_model)
    expected_normalized = normalize_model_name(alias_resolved)
    assert res.normalized == expected_normalized

    # --- Determinism: independent resolver + repeated call are identical -----
    assert resolver_b.resolve(external_model) == res
    assert resolver_a.resolve(external_model) == res

    # --- Layer-specific resolution invariants --------------------------------
    norm = expected_normalized
    if resolver_a.cache.is_valid_model(norm):
        # Known model from the dynamic cache.
        assert res.source == "cache"
        assert res.is_verified is True
        assert res.internal_id == to_runtime_model_id(norm)
    elif norm in HIDDEN_MODELS:
        # Hidden models still resolve to their internal Kiro id.
        assert res.source == "hidden"
        assert res.is_verified is True
        assert res.internal_id == to_runtime_model_id(HIDDEN_MODELS[norm])
    else:
        # Unknown -> deterministic optimistic pass-through.
        assert res.source == "passthrough"
        assert res.is_verified is False
        assert res.internal_id == to_runtime_model_id(norm)

    # --- Alias transparency: alias resolves like its (non-alias) target ------
    if external_model in ALIASES:
        target = ALIASES[external_model]
        assert target not in ALIASES  # guards the comparison below
        target_res = resolver_a.resolve(target)
        assert res.internal_id == target_res.internal_id
        assert res.normalized == target_res.normalized
        assert res.source == target_res.source
        assert res.is_verified == target_res.is_verified
