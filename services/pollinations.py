"""Check Pollinations API key balances via SOCKS5 proxy or direct."""

import logging

import aiohttp
from aiohttp_socks import ProxyConnector

logger = logging.getLogger(__name__)

BALANCE_URL = "https://gen.pollinations.ai/account/balance"
PROFILE_URL = "https://gen.pollinations.ai/account/profile"


def _get_connector(socks_port: int | None = None) -> ProxyConnector | None:
    if socks_port is None:
        return None
    return ProxyConnector.from_url(f"socks5://127.0.0.1:{socks_port}")


async def check_key_balance(api_key: str, socks_port: int | None = None) -> dict:
    """Check balance and profile for a Pollinations API key.

    Returns dict with: balance, tier, next_reset_at, or error.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=15)
    result = {}

    try:
        connector = _get_connector(socks_port)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(BALANCE_URL, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result["balance"] = data.get("balance", 0)
                else:
                    result["balance"] = None
                    result["error"] = f"Balance check failed: {resp.status}"

        connector2 = _get_connector(socks_port)
        async with aiohttp.ClientSession(connector=connector2) as session:
            async with session.get(PROFILE_URL, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result["tier"] = data.get("tier", "")
                    result["next_reset_at"] = data.get("nextResetAt", "")
    except Exception as e:
        logger.error("Error checking key balance: %s", e)
        result["error"] = str(e)

    return result


async def validate_key(api_key: str, socks_port: int | None = None) -> bool:
    """Check if an API key is valid by fetching its balance."""
    try:
        result = await check_key_balance(api_key, socks_port)
        return result.get("balance") is not None
    except Exception:
        return False
