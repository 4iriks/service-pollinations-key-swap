"""Transparent reverse proxy to gen.pollinations.ai with key rotation."""

import asyncio
import logging

import aiohttp
from aiohttp import web
from aiohttp_socks import ProxyConnector

from config import settings
from db import models
from services.vless import is_xray_running

logger = logging.getLogger(__name__)

UPSTREAM = "https://gen.pollinations.ai"

# Headers that should not be forwarded
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
})


def _get_connector(vless_config_index: int | None) -> ProxyConnector | None:
    if not is_xray_running() or vless_config_index is None:
        return None
    port = 10801 + vless_config_index
    return ProxyConnector.from_url(f"socks5://127.0.0.1:{port}")


async def _authenticate(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Validate Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, web.json_response(
            {"error": "Missing or invalid Authorization header. Use: Bearer <token>"},
            status=401,
        )

    token_value = auth[7:]
    token = await models.get_token_by_value(token_value)
    if token is None:
        return None, web.json_response({"error": "Invalid or revoked token"}, status=401)

    return token, None


def _forward_headers(request_headers: dict, api_key: str) -> dict:
    """Build headers for upstream request: replace auth, drop hop-by-hop."""
    headers = {}
    for k, v in request_headers.items():
        if k.lower() not in HOP_BY_HOP and k.lower() != "authorization":
            headers[k] = v
    headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _proxy_response(
    upstream_resp: aiohttp.ClientResponse, request: web.Request,
) -> web.StreamResponse:
    """Stream upstream response back to client."""
    # Forward response headers (skip hop-by-hop)
    resp_headers = {}
    for k, v in upstream_resp.headers.items():
        if k.lower() not in HOP_BY_HOP:
            resp_headers[k] = v

    content_type = upstream_resp.content_type or "application/octet-stream"

    # Check if response should be streamed (SSE or chunked)
    is_stream = (
        "text/event-stream" in content_type
        or upstream_resp.headers.get("Transfer-Encoding") == "chunked"
    )

    if is_stream:
        response = web.StreamResponse(
            status=upstream_resp.status,
            headers=resp_headers,
        )
        response.content_type = content_type
        await response.prepare(request)
        async for chunk in upstream_resp.content.iter_any():
            await response.write(chunk)
        await response.write_eof()
        return response
    else:
        body = await upstream_resp.read()
        return web.Response(
            status=upstream_resp.status,
            body=body,
            headers=resp_headers,
        )


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """Universal proxy handler — forwards any request to Pollinations."""
    # Skip auth for health/status endpoints
    path = request.path
    if path == "/health":
        return await health(request)
    if path == "/status":
        return await status(request)

    # Authenticate
    token, error = await _authenticate(request)
    if error:
        return error

    # Read request body (for POST/PUT/PATCH)
    body = None
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.read()

    # Build upstream URL
    upstream_url = UPSTREAM + path
    if request.query_string:
        upstream_url += "?" + request.query_string

    # Try keys with rotation on 402
    tried_keys: set[int] = set()
    for _attempt in range(3):
        key_data = await models.get_active_key(settings.balance_threshold, exclude_ids=tried_keys)
        if not key_data:
            break
        tried_keys.add(key_data["id"])

        headers = _forward_headers(request.headers, key_data["key"])
        vless_idx = key_data.get("vless_config_index")
        connector = _get_connector(vless_idx)

        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.request(
                    method=request.method,
                    url=upstream_url,
                    headers=headers,
                    data=body,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as upstream_resp:
                    if upstream_resp.status == 402:
                        logger.warning("Key #%d exhausted (402), swapping", key_data["id"])
                        await models.deactivate_key(key_data["id"])
                        await models.update_key_balance(key_data["id"], 0)
                        await models.add_request_log(
                            path, request.method, "key_exhausted",
                            key_data["id"], token["id"],
                        )
                        continue

                    # Log and return response
                    status_str = "ok" if upstream_resp.status < 400 else f"error_{upstream_resp.status}"
                    await models.add_request_log(
                        path, request.method, status_str,
                        key_data["id"], token["id"],
                    )
                    return await _proxy_response(upstream_resp, request)

        except Exception as e:
            logger.error("Proxy error (key #%d): %s", key_data["id"], e)
            await models.add_request_log(
                path, request.method, "exception",
                key_data["id"], token["id"],
            )
            return web.json_response({"error": "Upstream connection failed"}, status=502)

    await models.add_request_log(path, request.method, "no_keys", token_id=token["id"])
    return web.json_response({"error": "No active API keys available"}, status=503)


async def health(request: web.Request) -> web.Response:
    """GET /health"""
    keys_stats = await models.get_keys_stats()
    return web.json_response({
        "status": "ok",
        "active_keys": keys_stats["active"],
        "xray": is_xray_running(),
    })


async def status(request: web.Request) -> web.Response:
    """GET /status"""
    keys = await models.get_all_keys()
    return web.json_response({
        "keys": [
            {
                "masked": k["key"][:6] + "...",
                "balance": k["pollen_balance"],
                "active": bool(k["is_active"]),
                "vless": k.get("vless_remark") or None,
            }
            for k in keys
        ],
    })


def create_app() -> web.Application:
    app = web.Application()
    # Explicit routes for health/status (no auth)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    # Catch-all proxy for everything else
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    return app
