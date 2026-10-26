"""
Rate limiting, keyed on the client's real address.

Getting this right on Railway took measuring the live edge, logging the raw
address headers the app actually receives and checking them against Railway's
own HTTP logs (which record the connecting client as srcIp). The obvious
choices are all wrong here, and so was a plausible-sounding one:

  * `request.client.host` is the TCP peer, Railway's internal mesh
    (100.64.0.x), which rotates per request — a fresh bucket every time, so no
    limit ever fills. This is what slowapi's get_remote_address returned.
  * the RIGHTMOST `X-Forwarded-For` entry is the edge node, and Railway spreads
    requests across edge nodes, so it rotates too. "Rightmost public" is right
    for a single trusted proxy; Railway is two hops, so it picks the wrong one.
  * `X-Envoy-External-Address` sounded like the answer but is ALWAYS EMPTY on
    this platform — keying on it did nothing, every request fell through.

What the edge actually sends, measured on a live request whose true client was
known (223.x matched srcIp in the edge log):

  * `X-Real-IP` = exactly the client, on every request.
  * `X-Forwarded-For` = "client, edge" — client leftmost, edge rightmost (the
    rotating one).

And, decisively for trust: the edge OVERWRITES both of those headers (and the
envoy one) rather than appending — a request forging all three arrived with
every forged value discarded and the real client in their place. So on Railway
`X-Real-IP` and the leftmost XFF entry are the real client AND cannot be forged
by the caller. Key on `X-Real-IP`, fall back to the leftmost XFF entry, and only
then to the rotating peer — which is the degraded state (limits spread several
-fold across mesh addresses, not tightened), logged loudly so it is never
silent. This assumes the Railway edge in front of the app; if it is ever moved
behind a proxy that forwards client-supplied X-Real-IP instead of overwriting
it, this must be revisited.

Storage is slowapi's default in-process memory: one replica, one uvicorn worker
here (measured), so one store sees every request. A deploy resets the counters;
moving to REDIS_URL would fix that and survive a replica bump, but makes every
limited request depend on Redis being up, so it is left as a deliberate
follow-up rather than folded in.
"""

import ipaddress
import logging

from slowapi import Limiter
from starlette.requests import Request

logger = logging.getLogger(__name__)

# Log the chosen key and raw headers for the first few requests after boot, to
# confirm the key is the real client, then fall silent — not a permanent stream
# of client IPs.
_LOG_FIRST = 20
_logged = 0


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _leftmost_xff(xff: str) -> str | None:
    for entry in (e.strip() for e in xff.split(",")):
        if _valid_ip(entry):
            return entry          # client is leftmost; the edge overwrites the header
    return None


def client_ip(request: Request) -> str:
    """The real client address as Railway's edge reports it."""
    real_ip = request.headers.get("x-real-ip", "").strip()
    xff = request.headers.get("x-forwarded-for", "")
    peer = request.client.host if request.client else None

    global _logged
    if _valid_ip(real_ip):
        chosen = real_ip
    elif (left := _leftmost_xff(xff)) is not None:
        chosen = left
    else:
        chosen = peer or "127.0.0.1"
        # Both client headers gone. On Railway the peer rotates, so the limits
        # are now several-fold weaker, not stricter — alarm on every such
        # request, not once. Loopback is local/dev, where the peer is stable and
        # this is expected, so stay quiet there.
        if peer not in ("127.0.0.1", "::1", None):
            logger.warning(
                "[RATELIMIT] no x-real-ip or usable x-forwarded-for; keyed on peer=%s, "
                "which rotates on Railway — per-IP limits are DEGRADED. x-forwarded-for=%r",
                chosen, xff,
            )
        return chosen

    if _logged < _LOG_FIRST:
        _logged += 1
        logger.info(
            "[RATELIMIT] key=%s | x-real-ip=%r x-forwarded-for=%r "
            "x-envoy-external-address=%r peer=%s",
            chosen, real_ip, xff,
            request.headers.get("x-envoy-external-address", ""), peer,
        )
    return chosen


limiter = Limiter(key_func=client_ip, default_limits=["300/minute"])
