from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any


def _trusted_networks(settings: Any) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    values: Iterable[str] = getattr(settings.server, "trusted_proxy_cidrs", ())
    if isinstance(values, str):
        values = values.split(",")
    networks = []
    for value in values:
        value = value.strip()
        if value:
            networks.append(ipaddress.ip_network(value, strict=False))
    return tuple(networks)


def resolve_client_ip(request: Any, settings: Any) -> str:
    """Resolve a client address only when the immediate peer is trusted."""
    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer

    networks = _trusted_networks(settings)
    if not any(peer_ip in network for network in networks):
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded.strip():
        return peer
    values = forwarded.split(",")
    if any(not value.strip() for value in values):
        return peer
    try:
        chain = [ipaddress.ip_address(value.strip()) for value in values]
    except ValueError:
        return peer

    for address in reversed(chain):
        if not any(address in network for network in networks):
            return str(address)
    return peer
