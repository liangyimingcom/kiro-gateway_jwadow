"""
End-to-end live validation of the Kiro Session Key (API-key) authentication
credential source, using real keys.

Supported capability: AUTHENTICATION. Success = the key authenticates against
Kiro's identity endpoint (validate() -> True). Session keys are auth-only; this
does NOT attempt model serving.

Usage:
    python manual_apikey_e2e.py ksk_KEY1 [ksk_KEY2 ...]
"""
import asyncio
import sys

from kiro.auth import KiroAuthManager, AuthType


async def run_one(key: str) -> bool:
    print(f"\n=== Key {key[:8]}... ===")
    mgr = KiroAuthManager(api_key=key, region="us-east-1")

    # 1. auth type detection
    assert mgr.auth_type == AuthType.API_KEY, f"expected API_KEY, got {mgr.auth_type}"
    print(f"  auth_type            : {mgr.auth_type.value}  (OK)")

    # 2. endpoint routing
    assert "q.us-east-1.amazonaws.com" in mgr.api_host
    print(f"  identity endpoint    : {mgr.api_host}  (OK)")

    # 3. never-expiring
    assert mgr.is_token_expiring_soon() is False
    print(f"  is_token_expiring    : False  (OK)")

    # 4. get_access_token returns the key (no network)
    token = await mgr.get_access_token()
    assert token == key
    print(f"  get_access_token     : returns the key as Bearer  (OK)")

    # 5. LIVE authentication validation
    ok = await mgr.validate()
    print(f"  validate() [LIVE]    : {ok}")
    if mgr.profile_arn:
        print(f"  adopted profile_arn  : {mgr.profile_arn}")
    return ok


async def main() -> None:
    keys = sys.argv[1:]
    if not keys:
        print("Provide one or more ksk_ keys as arguments.")
        sys.exit(2)

    results = {}
    for k in keys:
        try:
            results[k[:8] + "..."] = await run_one(k)
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            results[k[:8] + "..."] = False

    print("\n" + "=" * 50)
    print("E2E AUTHENTICATION RESULT")
    print("=" * 50)
    all_ok = True
    for k, ok in results.items():
        print(f"  {k:14} -> {'AUTHENTICATED (PASS)' if ok else 'FAILED'}")
        all_ok = all_ok and ok
    print("=" * 50)
    print("OVERALL:", "PASS - all keys authenticate" if all_ok else "FAIL")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
