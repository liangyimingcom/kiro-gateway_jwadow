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
Kiro Gateway Configuration.

Centralized storage for all settings, constants, and mappings.
Loads environment variables and provides typed access to them.

Storage backend abstraction (AWS cloud-native refactor)
-------------------------------------------------------
Configuration values are no longer read directly via ``os.getenv``. Instead they
are sourced through an injected :class:`~kiro.backends.interfaces.ConfigProvider`
(see design.md "组件与接口"). By default a process-environment provider is used,
which is *behaviourally identical* to the previous ``os.getenv`` based loading
(precedence: environment variable > code default). At start-up the application
injects the provider produced by :func:`kiro.backends.factory.create_backend`
via :func:`set_config_provider`, which re-evaluates the module level constants
so that an alternative backend (e.g. SSM Parameter Store) can take effect.

All public constant names, default values and the model alias / hidden model
logic are preserved exactly, so existing call sites (``from kiro.config import
X``) keep working unchanged.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def _get_raw_env_value(var_name: str, env_file: str = ".env") -> Optional[str]:
    """
    Read variable value from .env file without processing escape sequences.
    
    This is necessary for correct handling of Windows paths where backslashes
    (e.g., D:\\Projects\\file.json) may be incorrectly interpreted
    as escape sequences (\\a -> bell, \\n -> newline, etc.).
    
    Args:
        var_name: Environment variable name
        env_file: Path to .env file (default ".env")
    
    Returns:
        Raw variable value or None if not found
    """
    env_path = Path(env_file)
    if not env_path.exists():
        return None
    
    try:
        # Read file as-is, without interpretation
        content = env_path.read_text(encoding="utf-8")
        
        # Search for variable considering different formats:
        # VAR="value" or VAR='value' or VAR=value
        # Pattern captures value with or without quotes
        pattern = rf'^{re.escape(var_name)}=(["\']?)(.+?)\1\s*$'
        
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            
            match = re.match(pattern, line)
            if match:
                # Return value as-is, without processing escape sequences
                return match.group(2)
    except Exception:
        pass
    
    return None


# ==================================================================================================
# Configuration Provider (Storage Backend Abstraction)
# ==================================================================================================


class _EnvConfigProvider:
    """
    Default :class:`~kiro.backends.interfaces.ConfigProvider` implementation that
    reads from the process environment.

    This is deliberately defined inline (rather than importing
    ``kiro.backends.local.LocalConfigProvider``) to avoid an import cycle
    (``kiro.config`` <-> ``kiro.backends.local.config_provider``). It is
    behaviourally equivalent to the previous direct ``os.getenv`` usage:
    precedence is environment variable > provided default.
    """

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        value = os.getenv(key)
        if value is None:
            return default
        return value

    def get_required(self, key: str) -> str:
        value = os.getenv(key)
        if value is None:
            # Imported lazily to avoid an import cycle at module load time.
            from kiro.backends.interfaces import MissingConfigError

            raise MissingConfigError(key)
        return value

    def get_namespace(self, prefix: str) -> Dict[str, str]:
        return {k: v for k, v in os.environ.items() if k.startswith(prefix)}

    def reload(self) -> None:
        load_dotenv()


# The active configuration provider. Defaults to the process-environment
# provider; replaced at start-up via set_config_provider() with the provider
# from the selected storage backend (local | aws).
_config_provider = _EnvConfigProvider()


def get_config_provider():
    """Return the active :class:`ConfigProvider`."""
    return _config_provider


def _cfg(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read ``key`` from the active configuration provider (env > default)."""
    return _config_provider.get(key, default)


# ==================================================================================================
# Non-configurable constants (not sourced from the environment)
# ==================================================================================================

# Server host default (default: 0.0.0.0 - listen on all interfaces)
# Use "127.0.0.1" to only allow local connections
DEFAULT_SERVER_HOST: str = "0.0.0.0"

# Server port default (default: 8000)
DEFAULT_SERVER_PORT: int = 8000

# ==================================================================================================
# Kiro API URL Templates
# ==================================================================================================

# URL for token refresh (Kiro Desktop Auth)
KIRO_REFRESH_URL_TEMPLATE: str = "https://prod.{region}.auth.desktop.kiro.dev/refreshToken"

# URL for token refresh (AWS SSO OIDC - used by kiro-cli)
AWS_SSO_OIDC_URL_TEMPLATE: str = "https://oidc.{region}.amazonaws.com/token"

# Host for main API (generateAssistantResponse)
# Universal endpoint for all regions (us-east-1, eu-central-1, etc.)
# See: https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/security-data-perimeter.html
# Fixed in issue #58 - codewhisperer.{region}.amazonaws.com doesn't exist for non-us-east-1 regions
KIRO_API_HOST_TEMPLATE: str = "https://runtime.{region}.kiro.dev"

# Host for Q API (ListAvailableModels)
KIRO_Q_HOST_TEMPLATE: str = "https://runtime.{region}.kiro.dev"

# ==================================================================================================
# Token Settings
# ==================================================================================================

# Time before token expiration when refresh is needed (in seconds)
# Default 10 minutes - refresh token in advance to avoid errors
TOKEN_REFRESH_THRESHOLD: int = 600

# ==================================================================================================
# Retry Configuration
# ==================================================================================================

# Maximum number of retry attempts on errors
MAX_RETRIES: int = 3

# Base delay between attempts (seconds)
# Uses exponential backoff: delay * (2 ** attempt)
BASE_RETRY_DELAY: float = 1.0

# ==================================================================================================
# Hidden Models Configuration
# ==================================================================================================

# Hidden models - not returned by Kiro /ListAvailableModels API but still functional.
# These ARE shown in our /v1/models endpoint!
# Use dot format for consistency with API models.
#
# Format: "display_name" → "internal_kiro_id"
# Display names use dots (e.g., "claude-3.7-sonnet") for consistency with Kiro API.
#
# Why "hidden"? These models work but are not advertised by Kiro's /ListAvailableModels.
# We expose them to our users because they're useful.
HIDDEN_MODELS: Dict[str, str] = {
    # Claude 3.7 Sonnet - legacy model, maps to "auto" on new runtime endpoint
    # "claude-3.7-sonnet": "auto",
}

# ==================================================================================================
# Model Aliases Configuration
# ==================================================================================================

# Model aliases - custom names that map to real model IDs.
# This feature allows creating alternative names for models to avoid namespace conflicts
# with IDE-specific model names (e.g., Cursor's "auto" model).
#
# Format: {"alias_name": "real_model_id"}
# - alias_name: The name that will appear in /v1/models and can be used in requests
# - real_model_id: The actual model ID that will be sent to Kiro API
#
# Use cases:
# - Avoid conflicts with IDE-specific model names (e.g., Cursor's "auto")
# - Create user-friendly shortcuts (e.g., "my-opus" → "claude-opus-4.5")
# - Support legacy model names from other providers
#
# Example:
#   MODEL_ALIASES = {
#       "auto-kiro": "auto",
#       "my-opus": "claude-opus-4.5",
#       "gpt-5": "claude-sonnet-4.5"
#   }
#
# Default: {"auto-kiro": "auto"} to avoid Cursor IDE conflict
MODEL_ALIASES: Dict[str, str] = {
    "auto-kiro": "auto",  # Default alias to avoid Cursor's "auto" model conflict
}

# Models to hide from /v1/models endpoint.
# These models still work when requested directly, but are not shown in the model list.
# This is useful when you want to show only aliases instead of original model names.
#
# Use case: Hide "auto" from list to show only "auto-kiro" alias, avoiding confusion.
#
# Example:
#   HIDDEN_FROM_LIST = ["auto", "claude-old-model"]
#
# Default: ["auto"] to show only "auto-kiro" alias
HIDDEN_FROM_LIST: List[str] = ["auto"]

# ==================================================================================================
# Fallback Models Configuration (DNS Failure Recovery)
# ==================================================================================================

# Fallback model list - used when /ListAvailableModels API is unreachable.
# This ensures basic functionality even with DNS/network issues.
#
# IMPORTANT: This list represents known models at the time of this gateway version.
# - Some models may not be available on your Kiro plan (e.g., Opus on free tier)
# - New models released after this version won't appear here
# - Update gateway regularly to get the latest model list
FALLBACK_MODELS: List[Dict[str, str]] = [
    {"modelId": "auto"},
    {"modelId": "claude-sonnet-4"},
    {"modelId": "claude-sonnet-4.5"},
    {"modelId": "claude-sonnet-4.6"},
    {"modelId": "claude-haiku-4.5"},
    {"modelId": "claude-opus-4.5"},
    {"modelId": "claude-opus-4.6"},
    {"modelId": "claude-opus-4.7"},
    {"modelId": "deepseek-3.2"},
    {"modelId": "glm-5"},
    {"modelId": "minimax-m2.1"},
    {"modelId": "minimax-m2.5"},
    {"modelId": "qwen3-coder-next"},
]

# ==================================================================================================
# Model Cache Settings
# ==================================================================================================

# Model cache TTL in seconds (1 hour)
MODEL_CACHE_TTL: int = 3600

# Default maximum number of input tokens
DEFAULT_MAX_INPUT_TOKENS: int = 200000

# ==================================================================================================
# Fake Reasoning - static tag configuration (not sourced from the environment)
# ==================================================================================================

# List of opening tags to detect thinking blocks.
# The parser will look for any of these tags at the start of the response.
# Order matters - first match wins.
FAKE_REASONING_OPEN_TAGS: List[str] = ["<thinking>", "<think>", "<reasoning>", "<thought>"]

# ==================================================================================================
# Application Version
# ==================================================================================================

APP_VERSION: str = "2.4.dev.13"
APP_TITLE: str = "Kiro Gateway"
APP_DESCRIPTION: str = "Proxy gateway for Kiro API (Amazon Q Developer / AWS CodeWhisperer). OpenAI and Anthropic compatible. Made by @jwadow"


def _load_config() -> None:
    """
    (Re)evaluate all environment-sourced configuration constants from the active
    :class:`ConfigProvider`.

    Called once at import time with the default process-environment provider and
    again whenever a different provider is injected via
    :func:`set_config_provider` (so an alternative backend can take effect). All
    constant names and default values are preserved exactly.
    """
    global SERVER_HOST, SERVER_PORT, PROXY_API_KEY, VPN_PROXY_URL
    global REFRESH_TOKEN, PROFILE_ARN, REGION, KIRO_CREDS_FILE, KIRO_CLI_DB_FILE
    global SQLITE_READONLY, TOOL_DESCRIPTION_MAX_LENGTH, TRUNCATION_RECOVERY
    global LOG_LEVEL, LOG_FORMAT, FIRST_TOKEN_TIMEOUT, STREAMING_READ_TIMEOUT, FIRST_TOKEN_MAX_RETRIES
    global DEBUG_MODE, DEBUG_DIR
    global FAKE_REASONING_ENABLED, FAKE_REASONING_MAX_TOKENS, FAKE_REASONING_BUDGET_CAP
    global FAKE_REASONING_HANDLING, FAKE_REASONING_INITIAL_BUFFER_SIZE
    global KIRO_MAX_PAYLOAD_BYTES, AUTO_TRIM_PAYLOAD, WEB_SEARCH_ENABLED
    global ACCOUNT_SYSTEM, ACCOUNTS_CONFIG_FILE, ACCOUNTS_STATE_FILE
    global ACCOUNT_RECOVERY_TIMEOUT, ACCOUNT_MAX_BACKOFF_MULTIPLIER
    global ACCOUNT_PROBABILISTIC_RETRY_CHANCE, ACCOUNT_CACHE_TTL, STATE_SAVE_INTERVAL_SECONDS

    # ==============================================================================================
    # Server Settings
    # ==============================================================================================

    # Server host (default: 0.0.0.0 - listen on all interfaces)
    # Use "127.0.0.1" to only allow local connections
    SERVER_HOST = _cfg("SERVER_HOST", DEFAULT_SERVER_HOST)

    # Server port (default: 8000)
    # Can be overridden by CLI: python main.py --port 9000
    # Or by uvicorn directly: uvicorn main:app --port 9000
    SERVER_PORT = int(_cfg("SERVER_PORT", str(DEFAULT_SERVER_PORT)))

    # ==============================================================================================
    # Proxy Server Settings
    # ==============================================================================================

    # API key for proxy access (clients must pass it in Authorization header)
    PROXY_API_KEY = _cfg("PROXY_API_KEY", "my-super-secret-password-123")

    # ==============================================================================================
    # VPN/Proxy Settings for Kiro API Access
    # ==============================================================================================

    # VPN/Proxy URL for accessing Kiro API through a proxy server.
    # Leave empty to connect directly (default). Supports HTTP and SOCKS5.
    VPN_PROXY_URL = _cfg("VPN_PROXY_URL", "")

    # ==============================================================================================
    # Kiro API Credentials
    # ==============================================================================================

    # Refresh token for updating access token
    REFRESH_TOKEN = _cfg("REFRESH_TOKEN", "")

    # Profile ARN for AWS CodeWhisperer
    PROFILE_ARN = _cfg("PROFILE_ARN", "")

    # AWS SSO/auth region (default us-east-1)
    # This region is used for OIDC token refresh endpoint: https://oidc.{region}.amazonaws.com/token
    REGION = _cfg("KIRO_REGION", "us-east-1")

    # Path to credentials file (optional, alternative to .env)
    # Read directly from .env to avoid escape sequence issues on Windows
    # (e.g., \a in path D:\Projects\adolf is interpreted as bell character)
    _raw_creds_file = _get_raw_env_value("KIRO_CREDS_FILE") or _cfg("KIRO_CREDS_FILE", "")
    # Normalize path for cross-platform compatibility
    KIRO_CREDS_FILE = str(Path(_raw_creds_file)) if _raw_creds_file else ""

    # Path to kiro-cli SQLite database (optional, for AWS SSO OIDC authentication)
    _raw_cli_db_file = _get_raw_env_value("KIRO_CLI_DB_FILE") or _cfg("KIRO_CLI_DB_FILE", "")
    KIRO_CLI_DB_FILE = str(Path(_raw_cli_db_file)) if _raw_cli_db_file else ""

    # Disable SQLite write-back (read-only mode)
    # Default: false (write-back enabled)
    SQLITE_READONLY = _cfg("SQLITE_READONLY", "false").lower() in ("true", "1", "yes")

    # ==============================================================================================
    # Tool Description Handling (Kiro API Limitations)
    # ==============================================================================================

    # Maximum length of tool description in characters.
    # Descriptions longer than this limit will be moved to system prompt.
    # Set to 0 to disable (not recommended - will cause Kiro API errors).
    TOOL_DESCRIPTION_MAX_LENGTH = int(_cfg("TOOL_DESCRIPTION_MAX_LENGTH", "10000"))

    # ==============================================================================================
    # Truncation Recovery Settings
    # ==============================================================================================

    # Enable automatic truncation recovery (synthetic message injection)
    # Default: true (enabled)
    TRUNCATION_RECOVERY = _cfg("TRUNCATION_RECOVERY", "true").lower() in ("true", "1", "yes")

    # ==============================================================================================
    # Logging Settings
    # ==============================================================================================

    # Log level for the application
    # Available levels: TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL
    # Default: INFO (recommended for production)
    LOG_LEVEL = _cfg("LOG_LEVEL", "INFO").upper()

    # Log output format for the main application logs written to stdout.
    #   - "json":   one structured JSON object per line (for CloudWatch ingestion,
    #               Requirements 8.1 / 8.7).
    #   - "pretty": human-readable single-line text (development).
    #   - "auto":   "json" when running on the AWS storage backend
    #               (STORAGE_BACKEND=aws), otherwise "pretty".
    # Regardless of format, secret values are redacted from log output
    # (Requirements 6.5).
    LOG_FORMAT = _cfg("LOG_FORMAT", "auto").lower()

    # ==============================================================================================
    # First Token Timeout Settings (Streaming Retry)
    # ==============================================================================================

    # Timeout for waiting for the first token from the model (in seconds).
    # Default: 15 seconds.
    FIRST_TOKEN_TIMEOUT = float(_cfg("FIRST_TOKEN_TIMEOUT", "15"))

    # Read timeout for streaming responses (in seconds).
    # Default: 300 seconds (5 minutes).
    STREAMING_READ_TIMEOUT = float(_cfg("STREAMING_READ_TIMEOUT", "300"))

    # Maximum number of attempts on first token timeout.
    # Default: 3 attempts
    FIRST_TOKEN_MAX_RETRIES = int(_cfg("FIRST_TOKEN_MAX_RETRIES", "3"))

    # ==============================================================================================
    # Debug Settings
    # ==============================================================================================

    # Debug logging mode:
    # - off: disabled (default)
    # - errors: save logs only for failed requests (4xx, 5xx)
    # - all: save logs for every request (overwrites on each request)
    _debug_mode_raw = (_cfg("DEBUG_MODE", "") or "").lower()
    if _debug_mode_raw in ("off", "errors", "all"):
        DEBUG_MODE = _debug_mode_raw
    else:
        DEBUG_MODE = "off"

    # Directory for debug log files
    DEBUG_DIR = _cfg("DEBUG_DIR", "debug_logs")

    # ==============================================================================================
    # Fake Reasoning Settings (Extended Thinking via Tag Injection)
    # ==============================================================================================

    # Enable fake reasoning - injects special tags into requests to enable model reasoning.
    # Default: true (enabled) - provides premium experience out of the box
    _fake_reasoning_raw = (_cfg("FAKE_REASONING", "") or "").lower()
    # Default is True - if env var is not set or empty, enable fake reasoning
    FAKE_REASONING_ENABLED = _fake_reasoning_raw not in ("false", "0", "no", "disabled", "off")

    # Maximum thinking length in tokens (default budget when client doesn't specify).
    # Default: 4000 tokens
    FAKE_REASONING_MAX_TOKENS = int(_cfg("FAKE_REASONING_MAX_TOKENS", "4000"))

    # Maximum budget cap for fake reasoning when client sends thinking budget.
    # Default: 10000 tokens (2.5x default budget of 4000)
    FAKE_REASONING_BUDGET_CAP = int(_cfg("FAKE_REASONING_BUDGET_CAP", "10000"))

    # How to handle the thinking block in responses:
    # Default: "as_reasoning_content"
    _fake_reasoning_handling_raw = (_cfg("FAKE_REASONING_HANDLING", "as_reasoning_content") or "").lower()
    if _fake_reasoning_handling_raw in ("as_reasoning_content", "remove", "pass", "strip_tags"):
        FAKE_REASONING_HANDLING = _fake_reasoning_handling_raw
    else:
        FAKE_REASONING_HANDLING = "as_reasoning_content"

    # Maximum size of initial buffer for tag detection (characters).
    # Default: 20 characters (enough for longest tag + some whitespace)
    FAKE_REASONING_INITIAL_BUFFER_SIZE = int(_cfg("FAKE_REASONING_INITIAL_BUFFER_SIZE", "20"))

    # ==============================================================================================
    # Payload Size Guard Settings
    # ==============================================================================================

    # Payload size limit in bytes (Kiro API rejects > ~615KB with cryptic 400 error)
    # Default 600KB provides safety margin below the ~615KB hard limit
    KIRO_MAX_PAYLOAD_BYTES = int(_cfg("KIRO_MAX_PAYLOAD_BYTES", "600000"))

    # Auto-trim payload when over limit (default: false - disabled)
    AUTO_TRIM_PAYLOAD = _cfg("AUTO_TRIM_PAYLOAD", "false").lower() in ("true", "1", "yes")

    # ==============================================================================================
    # WebSearch Settings (MCP Tool Emulation)
    # ==============================================================================================

    # Enable web_search tool auto-injection (default: true)
    WEB_SEARCH_ENABLED = _cfg("WEB_SEARCH_ENABLED", "true").lower() in ("true", "1", "yes")

    # ==============================================================================================
    # Account System Settings
    # ==============================================================================================

    # Enable account system with failover (default: false)
    # When false: uses first account without failover (legacy mode)
    # When true: enables full failover loop with Circuit Breaker
    ACCOUNT_SYSTEM = _cfg("ACCOUNT_SYSTEM", "false").lower() in ("true", "1", "yes")

    # Path to credentials configuration file
    ACCOUNTS_CONFIG_FILE = _cfg("ACCOUNTS_CONFIG_FILE", "credentials.json")

    # Path to runtime state file
    ACCOUNTS_STATE_FILE = _cfg("ACCOUNTS_STATE_FILE", "state.json")

    # ==============================================================================================
    # Circuit Breaker Settings
    # ==============================================================================================

    # Base recovery timeout in seconds (for exponential backoff)
    # Actual timeout = BASE * 2^(failures - 1), capped at BASE * MAX_MULTIPLIER
    ACCOUNT_RECOVERY_TIMEOUT = int(_cfg("ACCOUNT_RECOVERY_TIMEOUT", "60"))

    # Maximum backoff multiplier (cap for exponential backoff)
    # With BASE=60s and MAX=1440, maximum cooldown is 60 * 1440 = 86400s = 1 day
    ACCOUNT_MAX_BACKOFF_MULTIPLIER = float(_cfg("ACCOUNT_MAX_BACKOFF_MULTIPLIER", "1440.0"))

    # Probabilistic retry chance for "broken" accounts (0.0 - 1.0)
    # Default: 0.1 (10% chance) - prevents permanent "stuck" state
    ACCOUNT_PROBABILISTIC_RETRY_CHANCE = float(_cfg("ACCOUNT_PROBABILISTIC_RETRY_CHANCE", "0.1"))

    # ==============================================================================================
    # Account Cache Settings
    # ==============================================================================================

    # Model cache TTL in seconds (12 hours)
    # Cache is refreshed only when account is used (not in background)
    ACCOUNT_CACHE_TTL = int(_cfg("ACCOUNT_CACHE_TTL", "43200"))

    # ==============================================================================================
    # State Persistence Settings
    # ==============================================================================================

    # Interval for periodic state.json saving in seconds
    STATE_SAVE_INTERVAL_SECONDS = int(_cfg("STATE_SAVE_INTERVAL_SECONDS", "10"))


def set_config_provider(provider) -> None:
    """
    Inject the active :class:`ConfigProvider` (used at start-up by the storage
    backend factory) and re-evaluate all environment-sourced configuration
    constants from it.

    With the default ``local`` backend this is behaviourally identical to the
    previous ``os.getenv`` based loading. With the ``aws`` backend it allows
    values from SSM Parameter Store / S3 to take effect (Requirements 5.1, 5.3,
    5.5).

    Args:
        provider: An object implementing the ``ConfigProvider`` protocol. When
            ``None``, the default process-environment provider is restored.
    """
    global _config_provider
    _config_provider = provider if provider is not None else _EnvConfigProvider()
    _load_config()


# Evaluate configuration once at import time using the default provider so that
# ``from kiro.config import X`` keeps working exactly as before.
_load_config()


def _warn_timeout_configuration():
    """
    Print warning if timeout configuration is suboptimal.
    Called at application startup.
    
    FIRST_TOKEN_TIMEOUT should be less than STREAMING_READ_TIMEOUT:
    - FIRST_TOKEN_TIMEOUT: time to wait for model to START responding
    - STREAMING_READ_TIMEOUT: time to wait BETWEEN chunks during streaming
    """
    if FIRST_TOKEN_TIMEOUT >= STREAMING_READ_TIMEOUT:
        import sys
        YELLOW = "\033[93m"
        RESET = "\033[0m"
        
        warning_text = f"""
{YELLOW}⚠️  WARNING: Suboptimal timeout configuration detected.
    
    FIRST_TOKEN_TIMEOUT ({FIRST_TOKEN_TIMEOUT}s) >= STREAMING_READ_TIMEOUT ({STREAMING_READ_TIMEOUT}s)
    
    These timeouts serve different purposes:
      - FIRST_TOKEN_TIMEOUT: time to wait for model to START responding (default: 15s)
      - STREAMING_READ_TIMEOUT: time to wait BETWEEN chunks during streaming (default: 300s)
    
    Recommendation: FIRST_TOKEN_TIMEOUT should be LESS than STREAMING_READ_TIMEOUT.
    
    Example configuration:
      FIRST_TOKEN_TIMEOUT=15
      STREAMING_READ_TIMEOUT=300{RESET}
"""
        print(warning_text, file=sys.stderr)


def get_kiro_refresh_url(region: str) -> str:
    """Return Kiro Desktop Auth token refresh URL for the specified region."""
    return KIRO_REFRESH_URL_TEMPLATE.format(region=region)


def get_aws_sso_oidc_url(region: str) -> str:
    """Return AWS SSO OIDC token URL for the specified region."""
    return AWS_SSO_OIDC_URL_TEMPLATE.format(region=region)


def get_kiro_api_host(region: str) -> str:
    """Return API host for the specified region."""
    return KIRO_API_HOST_TEMPLATE.format(region=region)


def get_kiro_q_host(region: str) -> str:
    """Return Q API host for the specified region."""
    return KIRO_Q_HOST_TEMPLATE.format(region=region)
