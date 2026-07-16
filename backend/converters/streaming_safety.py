import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse


LIVE_EDGE_MAX_AGE_S = 0.5
DEFAULT_MAX_MESSAGE_SIZE = 2 * 1024 * 1024
DEFAULT_OUTPUT_QUEUE_MAX_CHUNKS = 16
DEFAULT_HEARTBEAT_INTERVAL_S = 10.0
DEFAULT_HEARTBEAT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class BufferedInput:
    enqueued_at: float
    payload: bytes


@dataclass(frozen=True)
class FatalOutput:
    error: RuntimeError


def validate_streaming_endpoint(ws_url: str, api_key: str) -> None:
    parsed = urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("Streaming endpoint must be a valid ws:// or wss:// URL")

    hostname = parsed.hostname.rstrip(".").lower()
    is_loopback = hostname == "localhost" or hostname.endswith(".localhost")
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False

    if not is_loopback and parsed.scheme != "wss":
        raise ValueError("Remote streaming endpoints must use wss://")
    if not is_loopback and not api_key.strip():
        raise ValueError("Remote streaming endpoints require a nonempty API key")
