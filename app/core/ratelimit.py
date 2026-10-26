"""
Rate limiting, keyed on the client's real address.

Getting this address right on Railway took measuring the live edge, because the
obvious choices are both wrong here:

  * `request.client.host` is the TCP peer, which is Railway's internal mesh
    (100.64.0.x) and rotates per request — a fresh bucket every time, so the
    limit never trips. This is what slowapi's get_remote_address returned, and
    why every per-IP limit was decorative.
  * the RIGHTMOST `X-Forwarded-For` entry is the edge node's address, and
    Railway spreads requests across edge nodes, so it rotates too. The
    "rightmost public" rule is correct for a single trusted proxy; Railway is
    two hops (edge -> app), so it picks the wrong one.

What the edge actually knows — verified against Railway's own HTTP logs, where a
request from a known client logged that client's real address as srcIp — reaches
the app as `X-Envoy-External-Address`, which Railway's Envoy sets to the real
client and a client cannot forge (Envoy overwrites any incoming copy). That is
the key.

`X-Forwarded-For` is deliberately NOT used for the key. Each proxy appends the
address it received from, so a client that sends its own `X-Forwarded-For`
header pushes an attacker-chosen value onto the LEFT of the chain and can vary
it per request — a fresh bucket every time, the very evasion this is meant to
stop. The leftmost entry is therefore the one value in the request that must not
be trusted.

The Envoy header is thus not just preferred but required for the limits to mean
anything HERE. The fallback — the real TCP peer, `request.client.host` — is
unspoofable, but on Railway it is the internal mesh address, which ROTATES per
request (measured: five distinct 100.64.0.x peers in one page of access logs).
So if the Envoy header is ever missing, the fallback does not tighten the limit
into one bucket; it SPREADS it across mesh addresses and weakens it several-fold
silently. That is why its absence is logged at WARNING on every request, not
quietly: it is the condition under which rate limiting stops working. XFF is
logged alongside only so the chain can be read when diagnosing that case.

Storage is slowapi's default in-process memory. Measured: the service runs a
single replica and a single uvicorn worker, so one store sees every request and
the counts are whole. The one cost is that a deploy resets the counters; moving
to REDIS_URL would fix that and also survive a future replica bump, but it makes
every limited request depend on Redis being up, so it is left as a deliberate
follow-up rather than folded in here.
"""

import ipaddress
import logging

from slowapi import Limiter
from starlette.requests import Request

logger = logging.getLogger(__name__)

# Log the raw address headers for the first few requests after a deploy, then
# fall silent. Enough to confirm against ground truth which header carries the
# client, without turning client IPs into a permanent log stream.
_LOG_FIRST = 20
_logged = 0


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """The real client address, as the Railway edge reports it."""
    envoy = request.headers.get("x-envoy-external-address", "").strip()
    xff = request.headers.get("x-forwarded-for", "")

    peer = request.client.host if request.client else None

    if _valid_ip(envoy):
        chosen = envoy                                  # unforgeable: Envoy sets it
        global _logged
        if _logged < _LOG_FIRST:
            _logged += 1
            logger.info(
                "[RATELIMIT] key=%s via x-envoy-external-address | "
                "x-forwarded-for=%r x-real-ip=%r peer=%s",
                chosen, xff, request.headers.get("x-real-ip", ""), peer,
            )
    else:
        chosen = peer or "127.0.0.1"
        # The header the limits depend on is gone. On Railway the peer rotates,
        # so the limits are now several-fold weaker, not stricter — alarm on it
        # loudly and on every request, not once. Loopback is local/dev, where the
        # peer is stable and this is expected, so stay quiet there.
        if peer not in ("127.0.0.1", "::1", None):
            logger.warning(
                "[RATELIMIT] x-envoy-external-address missing/invalid (%r); keyed on "
                "peer=%s, which rotates on Railway — per-IP limits are DEGRADED. "
                "x-forwarded-for=%r x-real-ip=%r",
                envoy, chosen, xff, request.headers.get("x-real-ip", ""),
            )
    return chosen


# The global default was 100/minute, but it never actually applied — the key
# rotated, so no bucket ever filled. Fixing the key turns it on for the first
# time, against every endpoint at once. An active admin session (dashboard +
# orders + deliveries, each ~13 calls on load, plus 15-30s polling) can brush
# 100/min, so that number would now 429 a legitimate user. 300/min (5 req/s
# sustained per client) stays clear of real human use while still capping a
# flood of the public read endpoints that carry no explicit limit of their own.
# The strict per-route limits (5/min on the auth paths, etc.) apply on top and
# win where they are tighter.
limiter = Limiter(key_func=client_ip, default_limits=["300/minute"])
