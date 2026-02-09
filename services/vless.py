"""Parse VLESS URLs, generate XRAY config, manage XRAY process."""

import asyncio
import json
import logging
from urllib.parse import parse_qs, unquote

logger = logging.getLogger(__name__)

_xray_process: asyncio.subprocess.Process | None = None


def parse_vless_url(url: str) -> dict | None:
    """Parse vless://UUID@host:port?params#remark into a dict."""
    url = url.strip()
    if not url.startswith("vless://"):
        logger.error("Not a VLESS URL: %s", url[:30])
        return None

    remark = ""
    if "#" in url:
        url, remark = url.rsplit("#", 1)
        remark = unquote(remark)

    without_scheme = url[len("vless://"):]
    if "@" not in without_scheme:
        logger.error("Invalid VLESS URL format (no @)")
        return None

    uuid_part, rest = without_scheme.split("@", 1)

    if "?" in rest:
        host_port, query_string = rest.split("?", 1)
    else:
        host_port, query_string = rest, ""

    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        port = int(port_str)
    else:
        host = host_port
        port = 443

    params = {}
    if query_string:
        parsed = parse_qs(query_string)
        params = {k: v[0] for k, v in parsed.items()}

    return {
        "uuid": uuid_part,
        "host": host,
        "port": port,
        "remark": remark,
        "type": params.get("type", "tcp"),
        "security": params.get("security", "none"),
        "sni": params.get("sni", ""),
        "fp": params.get("fp", ""),
        "pbk": params.get("pbk", ""),
        "sid": params.get("sid", ""),
        "path": params.get("path", ""),
        "host_header": params.get("host", ""),
        "serviceName": params.get("serviceName", ""),
        "flow": params.get("flow", ""),
        "alpn": params.get("alpn", ""),
        "encryption": params.get("encryption", "none"),
    }


def _build_stream_settings(cfg: dict) -> dict:
    """Build streamSettings for XRAY outbound."""
    network = cfg["type"]
    security = cfg["security"]

    stream = {"network": network}

    if network == "ws":
        ws_settings = {}
        if cfg["path"]:
            ws_settings["path"] = cfg["path"]
        if cfg["host_header"]:
            ws_settings["headers"] = {"Host": cfg["host_header"]}
        stream["wsSettings"] = ws_settings
    elif network == "grpc":
        grpc_settings = {}
        if cfg["serviceName"]:
            grpc_settings["serviceName"] = cfg["serviceName"]
        stream["grpcSettings"] = grpc_settings
    elif network == "tcp":
        stream["tcpSettings"] = {}

    if security == "tls":
        tls_settings = {}
        if cfg["sni"]:
            tls_settings["serverName"] = cfg["sni"]
        if cfg["fp"]:
            tls_settings["fingerprint"] = cfg["fp"]
        if cfg["alpn"]:
            tls_settings["alpn"] = cfg["alpn"].split(",")
        stream["security"] = "tls"
        stream["tlsSettings"] = tls_settings
    elif security == "reality":
        reality_settings = {}
        if cfg["sni"]:
            reality_settings["serverName"] = cfg["sni"]
        if cfg["fp"]:
            reality_settings["fingerprint"] = cfg["fp"]
        if cfg["pbk"]:
            reality_settings["publicKey"] = cfg["pbk"]
        if cfg["sid"]:
            reality_settings["shortId"] = cfg["sid"]
        stream["security"] = "reality"
        stream["realitySettings"] = reality_settings
    else:
        stream["security"] = "none"

    return stream


def generate_xray_config(vless_urls: list[str]) -> dict:
    """Generate XRAY config with SOCKS5 inbounds and VLESS outbounds."""
    configs = []
    for url in vless_urls:
        parsed = parse_vless_url(url)
        if parsed:
            configs.append(parsed)

    if not configs:
        return {}

    inbounds = []
    outbounds = []

    for i, cfg in enumerate(configs):
        tag = f"vless-{i}"
        socks_tag = f"socks-in-{i}"
        port = 10801 + i

        inbounds.append({
            "tag": socks_tag,
            "port": port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": True},
        })

        outbound = {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": cfg["host"],
                    "port": cfg["port"],
                    "users": [{
                        "id": cfg["uuid"],
                        "encryption": cfg["encryption"],
                    }],
                }],
            },
            "streamSettings": _build_stream_settings(cfg),
        }

        if cfg["flow"]:
            outbound["settings"]["vnext"][0]["users"][0]["flow"] = cfg["flow"]

        outbounds.append(outbound)

    rules = []
    for i in range(len(configs)):
        rules.append({
            "type": "field",
            "inboundTag": [f"socks-in-{i}"],
            "outboundTag": f"vless-{i}",
        })

    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"rules": rules},
    }


def save_xray_config(vless_urls: list[str], path: str = "/tmp/xray_config.json") -> str | None:
    """Generate and save XRAY config to file."""
    config = generate_xray_config(vless_urls)
    if not config:
        return None

    with open(path, "w") as f:
        json.dump(config, f, indent=2)

    logger.info("XRAY config saved to %s with %d tunnels", path, len(config["inbounds"]))
    return path


async def start_xray(config_path: str = "/tmp/xray_config.json") -> bool:
    """Start XRAY subprocess. Returns True on success."""
    global _xray_process
    await stop_xray()

    try:
        _xray_process = await asyncio.create_subprocess_exec(
            "/usr/local/bin/xray", "run", "-config", config_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Give it a moment to start
        await asyncio.sleep(1)
        if _xray_process.returncode is not None:
            stderr = await _xray_process.stderr.read()
            logger.error("XRAY failed to start: %s", stderr.decode()[:500])
            _xray_process = None
            return False
        logger.info("XRAY started (pid=%d)", _xray_process.pid)
        return True
    except FileNotFoundError:
        logger.error("XRAY binary not found at /usr/local/bin/xray")
        return False
    except Exception as e:
        logger.error("Failed to start XRAY: %s", e)
        return False


async def stop_xray():
    """Stop XRAY subprocess if running."""
    global _xray_process
    if _xray_process is not None:
        try:
            _xray_process.terminate()
            await asyncio.wait_for(_xray_process.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                _xray_process.kill()
            except ProcessLookupError:
                pass
        logger.info("XRAY stopped")
        _xray_process = None


_xray_tunnel_count: int = 0


def is_xray_running() -> bool:
    """Check if XRAY process is alive."""
    return _xray_process is not None and _xray_process.returncode is None


def get_xray_tunnel_count() -> int:
    """Return the number of tunnels XRAY was started with."""
    return _xray_tunnel_count


async def restart_xray(vless_urls: list[str]) -> bool:
    """Regenerate config and restart XRAY."""
    global _xray_tunnel_count
    path = save_xray_config(vless_urls)
    if not path:
        logger.warning("No VLESS configs, XRAY not started")
        return False
    ok = await start_xray(path)
    if ok:
        _xray_tunnel_count = len(vless_urls)
    return ok
