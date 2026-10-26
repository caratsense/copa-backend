"""
Rate limiting, keyed on the client's real address.

The app only ever sees Railway's proxy as its TCP peer, and that peer is not
one stable address - it changes from connection to connection. Keying limits on
it (slowapi's get_remote_address) therefore put almost every request into a
fresh bucket: on the live API, twenty /auth/verify-otp attempts in sixteen
seconds from a single machine were all answered, against a limit of five a
minute. Every per-IP limit in the API was decorative.

The client's address is in X-Forwarded-For instead. It is read from the RIGHT:
each proxy appends the address it received the request from, so the entries a
client wrote itself are on the left, where a spoofed value would land, and the
one the edge wrote is the rightmost public address. Private and carrier-grade
NAT entries (Railway's internal hops) are skipped on the way, so an internal
proxy appending its own address cannot become everyone's key.

One Limiter for the whole app. Routes used to build their own, which meant
three in-memory stores and three places to get the key function wrong.
"""

import ipaddress

from slowapi import Limiter
from starlette.requests import Request

# Carrier-grade NAT, which is what Railway's internal network uses. Python does
# not count it as private, so it is excluded explicitly.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_public(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified
                or (ip.version == 4 and ip in _CGNAT))


def client_ip(request: Request) -> str:
    """The address the request came from, as reported by the edge proxy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    for entry in reversed([e.strip() for e in forwarded.split(",")]):
        if _is_public(entry):
            return entry
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


limiter = Limiter(key_func=client_ip, default_limits=["100/minute"])
