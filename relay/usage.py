"""Anthropic `/api/oauth/usage` client: fetch a seat's real usage window, parse the
normalized `limits[]`, and answer the two questions the rest of the tool needs:
"is this seat near its cap right now?" and "when will it next have headroom?".

Ground truth (confirmed live, DESIGN.md §0):
    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <accessToken>        (from {seat}/.credentials.json -> claudeAiOauth.accessToken)
    anthropic-beta: oauth-2025-04-20
    User-Agent: claude-code/2.1

Response shape (subset we rely on):
    { "five_hour": {"utilization": <0-100>, "resets_at": "<ISO8601+tz>", ...},
      "seven_day":  {...},
      "limits": [ {"kind": "session"|"weekly_all"|"weekly_scoped", "group": <str>,
                   "percent": <0-100>, "severity": "normal"|..., "resets_at": "<ISO8601+tz>",
                   "scope": {"model": {"display_name": <str>}}, "is_active": <bool>}, ... ] }

The rotation signal is the `is_active` limit with `kind == "session"`: rotate this seat off
(or refuse to start on it) when its `percent >= high_pct` (default 90) OR `severity != "normal"`.
We also gate on `weekly_all` and any per-model `weekly_scoped` limit — an Opus-heavy tier can be
weekly-capped while the 5-hour session window is fine.

Only stdlib `urllib.request` is used. The access token is held in memory only for the
duration of one request; it is never logged, printed, or persisted (Invariant #5).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USER_AGENT = "claude-code/2.1"
ANTHROPIC_BETA = "oauth-2025-04-20"
DEFAULT_TIMEOUT_S = 15.0

# Fallback "active session percent (near) 100" default for `near_cap()` when no explicit
# threshold is supplied. In normal operation `gadkit.outcome()` always passes the CALLER's
# synthetic per-seat ceiling_pct explicitly (default 70, config.py) as `near_cap()`'s
# `threshold` — HIT_WALL is ceiling-relative, not a fixed global percent, so a seat configured
# with a low ceiling correctly reports HIT_WALL right at ITS ceiling instead of being
# misclassified as AGENT_DEAD_NONLIMIT until some unrelated near-100% value (the "dead zone"
# between a seat's real ceiling and 100% that early v1 versions had). This constant remains
# only as a conservative default for direct callers that don't have a per-seat ceiling handy.
NEAR_CAP_PCT = 99.0

# Weekly limits (weekly_all / per-model weekly_scoped) are gated NEAR the REAL weekly cap, NOT
# against the synthetic 5-hour `ceiling_pct`. The synthetic ceiling is a 5h-SESSION reservation
# only (the operator's "leave headroom for one-off web use" knob); it must not reach across to
# the much-longer weekly window. Without this split, a seat idle on its 5h window but normally
# mid-week (e.g. weekly 33%) is wrongly rotated off by a low 5h ceiling like 15%.
WEEKLY_CEILING_PCT = 95.0


class UsageError(RuntimeError):
    """Base class for all usage-fetch failures. Never includes the token in its message."""


class NeedsLoginError(UsageError):
    """The seat has no usable `.credentials.json` — skip it, don't treat it as a hard error."""


class RateLimited(UsageError):
    """The usage endpoint itself returned 429. Callers should honor `retry_after` and prefer
    a cached reading over hammering the endpoint again immediately.
    """

    def __init__(self, retry_after_s: float | None):
        super().__init__(f"usage endpoint rate-limited us (Retry-After={retry_after_s!r}s)")
        self.retry_after_s = retry_after_s


class UsageFetchError(UsageError):
    """Any other HTTP or network failure talking to the usage endpoint. `status_code` carries the
    HTTP status when the failure was an HTTP error (e.g. 401 = token present but rejected — the
    seat needs a token refresh, which a `claude` launch performs), and is None for a pure network
    error. Callers that only need "did it work?" can keep catching `UsageError`; the field lets a
    read-only observer (the monitor) explain *why* a poll failed without changing control flow.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def is_auth_error(exc: UsageError) -> bool:
    """True if `exc` means the token was rejected (HTTP 401) — distinct from a network blip or
    rate limit. Such a seat is NOT permanently dead: a `claude` launch refreshes its token
    (runner.py's module docstring, confirmed live). Moved here (B8 audit fix, 2026-07-26) from
    `monitor.py`, which had the only copy, so `pick_seat()` can share the exact same predicate
    rather than re-deriving it or reaching into another module's private helper.
    """
    return isinstance(exc, UsageFetchError) and exc.status_code == 401


@dataclasses.dataclass(frozen=True)
class Limit:
    kind: str  # "session" | "weekly_all" | "weekly_scoped"
    group: str | None
    percent: float
    severity: str
    resets_at: str | None
    model_display_name: str | None
    is_active: bool

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> Limit:
        scope = obj.get("scope") or {}
        model = scope.get("model") or {} if isinstance(scope, dict) else {}
        return cls(
            kind=str(obj.get("kind", "")),
            group=obj.get("group"),
            percent=float(obj.get("percent", 0) or 0),
            severity=str(obj.get("severity", "normal")),
            resets_at=obj.get("resets_at"),
            model_display_name=model.get("display_name") if isinstance(model, dict) else None,
            is_active=bool(obj.get("is_active", False)),
        )


@dataclasses.dataclass(frozen=True)
class UsageSnapshot:
    five_hour: dict[str, Any]
    seven_day: dict[str, Any]
    limits: list[Limit]
    fetched_at: float  # time.time() the reading was taken (or cached from)

    @classmethod
    def from_json(cls, obj: dict[str, Any], fetched_at: float) -> UsageSnapshot:
        limits_raw = obj.get("limits")
        limits = [Limit.from_json(x) for x in limits_raw] if isinstance(limits_raw, list) else []
        return cls(
            five_hour=obj.get("five_hour") or {},
            seven_day=obj.get("seven_day") or {},
            limits=limits,
            fetched_at=fetched_at,
        )


def active_limit(usage: UsageSnapshot, kind: str) -> Limit | None:
    for limit in usage.limits:
        if limit.kind == kind and limit.is_active:
            return limit
    return None


def active_session_limit(usage: UsageSnapshot) -> Limit | None:
    return active_limit(usage, "session")


def session_utilization(usage: UsageSnapshot) -> float | None:
    """The raw 5-hour session utilization percent (0-100) from the `five_hour` gauge. This gauge
    is ALWAYS present in the endpoint response, unlike the normalized `limits[]` "session" entry,
    which the API only emits once the 5-hour window is a *binding* constraint. It is the ground
    truth the per-seat SYNTHETIC ceiling gates on: verified live 2026-07-19, a seat sitting at
    28% had NO active `limits[]` session entry (so `active_session_limit()` was None) yet was
    unambiguously 28% into its 5-hour window — gating on the normalized limit alone silently
    failed to rotate it off. Returns None only if the field is missing/unparseable.
    """
    raw = usage.five_hour.get("utilization")
    if raw is None:
        return None
    # `bool` must be rejected BEFORE `float()`: `isinstance(True, int)` is True in Python, so a
    # boolean would coerce to 1.0 — and on this 0-100 PERCENT gauge that reads as "1% used", i.e.
    # a nearly-empty window. The failure direction therefore points the wrong way: claude-relay
    # would keep dispatching work to a seat it should have rotated off, which is exactly the
    # missed-rotation outcome Invariant #2 exists to prevent. Found 2026-07-27 while building the
    # rate-limit calibration harness, whose own `_pct()` had the identical latent defect. Defensive
    # rather than observed: the endpoint has never sent a bool here, but it does send booleans in
    # many neighbouring fields (`spend.enabled`, `extra_usage.is_enabled`, ...), so a field rename
    # or a schema change putting one here is a realistic way to reach this.
    if isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def session_percent(usage: UsageSnapshot) -> float:
    """Best-available 5-hour session percent for ranking / start-cap gating: the raw `five_hour`
    utilization when present (ground truth), else the normalized active session limit's percent,
    else 0.0. Callers that rank/gate seats must use THIS rather than `active_session_limit`
    directly — the normalized session entry is frequently absent (see `session_utilization`), and
    treating that absence as 0% silently defeats the `start_margin` reservation.
    """
    util = session_utilization(usage)
    if util is not None:
        return util
    session = active_session_limit(usage)
    return session.percent if session else 0.0


def session_resets_at(usage: UsageSnapshot) -> dt.datetime | None:
    """When this seat's 5-hour session window resets, from the `five_hour` gauge (falling back to
    the normalized active session limit's `resets_at` if the gauge lacks one). Used by seat
    selection to spend PERISHABLE capacity first: a window about to reset will refresh its quota
    imminently, so its remaining headroom is "use it or lose it" and should be preferred over a
    seat whose window resets hours from now (whose capacity, once spent, stays spent for hours).
    Returns None if no reset time is available/parseable.
    """
    raw = usage.five_hour.get("resets_at")
    if isinstance(raw, str):
        parsed = _parse_iso8601(raw)
        if parsed is not None:
            return parsed
    session = active_session_limit(usage)
    if session is not None and session.resets_at:
        return _parse_iso8601(session.resets_at)
    return None


def weekly_limits(usage: UsageSnapshot) -> list[Limit]:
    """`weekly_all` plus any `weekly_scoped` (per-model) limits, active or not — pick_seat and
    rotate_off both need to see these even if `is_active` is false for the currently-selected
    model, since a different per-model scope could be the binding constraint.
    """
    return [limit for limit in usage.limits if limit.kind in ("weekly_all", "weekly_scoped")]


def rotate_off(
    usage: UsageSnapshot, high_pct: float, weekly_ceiling: float = WEEKLY_CEILING_PCT
) -> bool:
    """True if this seat should be rotated off / not started on. `high_pct` is the seat's
    SYNTHETIC 5-hour SESSION ceiling (per-seat, default 70): rotate off when the active session
    limit is at/above it or its severity is non-normal. Weekly limits (all/scoped) are gated
    SEPARATELY against `weekly_ceiling` (near the REAL weekly cap, default 95) — NOT the 5h
    ceiling — since the synthetic reservation applies only to the 5-hour window (DESIGN.md §4).
    """
    util = session_utilization(usage)
    if util is not None and util >= high_pct:
        return True
    # Secondary early-warning: the normalized session limit (when the API emits it as active)
    # carries a severity Anthropic sets — a non-"normal" severity means the window is flagged as
    # elevated even if raw utilization is still under the synthetic ceiling. `percent` here is
    # redundant with `util` above when both exist, kept for the case util is unexpectedly absent.
    session = active_session_limit(usage)
    if session is not None and (session.percent >= high_pct or session.severity != "normal"):
        return True
    for weekly in weekly_limits(usage):
        if weekly.percent >= weekly_ceiling:
            return True
    return False


def near_cap(usage: UsageSnapshot, threshold: float = NEAR_CAP_PCT) -> bool:
    """True if the active session limit is essentially exhausted right now (the HIT_WALL
    signal). Distinct from `rotate_off`, which fires much earlier (default 90%, OR any
    non-"normal" severity) so the loop hands a seat off well before hard exhaustion.

    Observed `severity` values, all live: `normal`, `warning` (at 82%, 2026-07-19), and `critical`
    (at 100% on a seat that had genuinely walled, 2026-07-27). `rotate_off()` gates on
    `severity != "normal"`, so `critical` routes correctly with no enum change needed — verified
    against a real walled seat, whose endpoint payload was:
        five_hour {"utilization": 100.0, "resets_at": "...T22:30:00Z"}
        limits[]  {"kind":"session","percent":100,"severity":"critical","is_active":true}
    and on which `session_utilization()`, `session_percent()`, `near_cap()` and
    `rotate_off(high=90)` all reported the wall correctly. That is the Invariant #2 primary path
    working as designed, and it is why the NDJSON `rate_limit_event` is only a supplementary signal.

    Percent-only, deliberately NOT severity-gated: a real live probe against this endpoint
    (2026-07-19) observed `severity: "warning"` at only 82% utilization — i.e. Anthropic's
    severity tiers are an early/soft signal (exactly what `rotate_off` should react to), not
    evidence the session is actually exhausted. Using severity here would make `outcome()`
    misclassify a merely-elevated seat as HIT_WALL far too early.
    """
    util = session_utilization(usage)
    if util is not None and util >= threshold:
        return True
    session = active_session_limit(usage)
    if session is not None and session.percent >= threshold:
        return True
    return any(w.percent >= WEEKLY_CEILING_PCT for w in weekly_limits(usage))


def earliest_reset(usage: UsageSnapshot) -> dt.datetime | None:
    """Earliest `resets_at` across the active session limit and any weekly limits — used to
    compute how long to sleep when no seat currently qualifies.
    """
    candidates: list[dt.datetime] = []
    # The 5h session reset from the raw `five_hour` gauge (via session_resets_at), NOT the
    # normalized active session limit — the latter is frequently absent (see session_utilization),
    # and when it is, this used to fall back to the WEEKLY reset and cool a seat off for DAYS
    # instead of until its 5h window reopens (observed live 2026-07-19: almas, rotated off at the
    # synthetic 5h ceiling with no active session limit, got a 2-day cooldown instead of ~5h).
    sess = session_resets_at(usage)
    if sess is not None:
        candidates.append(sess)
    for weekly in weekly_limits(usage):
        if weekly.resets_at:
            parsed = _parse_iso8601(weekly.resets_at)
            if parsed:
                candidates.append(parsed)
    return min(candidates) if candidates else None


def _parse_iso8601(value: str) -> dt.datetime | None:
    try:
        # Python's fromisoformat accepts "Z" only from 3.11+; normalize defensively anyway.
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_access_token(seat_dir: Path) -> str:
    cred_path = seat_dir / ".credentials.json"
    try:
        with cred_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise NeedsLoginError(f"seat {seat_dir.name} has no readable .credentials.json") from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not token:
        raise NeedsLoginError(f"seat {seat_dir.name} .credentials.json has no accessToken")
    return str(token)


def fetch_usage(seat_dir: Path, timeout: float = DEFAULT_TIMEOUT_S) -> UsageSnapshot:
    """Perform ONE live GET against the usage endpoint for this seat. Raises NeedsLoginError,
    RateLimited, or UsageFetchError on failure. Never logs the Authorization header.
    """
    parsed = fetch_usage_raw(seat_dir, timeout=timeout)
    return UsageSnapshot.from_json(parsed, fetched_at=time.time())


def fetch_usage_raw(seat_dir: Path, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """The same single live GET as `fetch_usage()`, but returning the endpoint's payload VERBATIM
    instead of the lossy `UsageSnapshot` projection.

    `UsageSnapshot.from_json()` keeps only the four fields the rotation logic needs, so any field
    Anthropic adds — or any enum value we have not yet seen — is silently dropped. The rate-limit
    calibration harness (`bin/rate-limit-probe`) needs the unprojected payload precisely because
    its job is to discover fields and enum values this module does not know about yet.

    Kept as the single HTTP implementation for both callers on purpose: duplicating the request
    would duplicate the token handling and the three-way error contract (Invariant #5 — the
    Authorization header must never reach a log — and Invariant #3's "may raise only the three
    documented exception types"), and a capture tool drifting from production auth behaviour is
    exactly how a harness ends up measuring something other than what production does.
    """
    token = _load_access_token(seat_dir)  # kept in a local variable only, never logged
    # USAGE_URL is our own fixed https://api.anthropic.com constant, not user input — the
    # flake8-bandit "audit url open" check is a false positive here.
    request = urllib.request.Request(  # noqa: S310
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            retry_after_s = float(retry_after) if retry_after and retry_after.isdigit() else None
            raise RateLimited(retry_after_s) from exc
        raise UsageFetchError(
            f"usage endpoint returned HTTP {exc.code} for seat {seat_dir.name}", status_code=exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise UsageFetchError(f"network error reaching usage endpoint: {exc.reason}") from exc
    except (OSError, http.client.HTTPException) as exc:
        # B7 audit fix: `urlopen()` wraps CONNECT-phase failures as URLError, but a failure
        # during the READ phase (after the connection is already established — a mid-stream RST,
        # a read timeout, a malformed status line) can arrive as a raw `TimeoutError`,
        # `ConnectionResetError`, or `http.client.BadStatusLine` instead, which is NOT a
        # `URLError` subclass and previously escaped every "never raises" handler here — one
        # middlebox RST would have killed a multi-day run (Invariant #3: this function may raise
        # only the three documented exception types, never anything else).
        raise UsageFetchError(f"network error reaching usage endpoint: {exc}") from exc
    finally:
        del token  # best-effort: drop the only local reference promptly

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise UsageFetchError("usage endpoint returned non-JSON body") from exc
    if not isinstance(parsed, dict):
        raise UsageFetchError("usage endpoint returned a non-object JSON body")
    return parsed


@dataclasses.dataclass
class _CacheEntry:
    snapshot: UsageSnapshot
    cached_at: float
    extra_ttl_s: float = 0.0  # bump when we honor a Retry-After


# B11 audit fix: `extra_ttl_s` accumulates on every consecutive 429 WITHOUT `cached_at` ever
# being refreshed, so a long rate-limited streak (each extension adding its own Retry-After on
# top of the last) measured 11.1h of staleness in one reproduction — the cache kept calling an
# hours-old reading "fresh" the whole time. Bounding it here caps how stale a served reading can
# ever become regardless of how many consecutive 429s occur, independent of the accumulation
# formula itself. 30 minutes is chosen as "long enough to ride out a real rate-limit backoff
# without re-hammering the endpoint every poll_ttl," short enough that a usage percent this old
# is still a reasonable (if imperfect) basis for a rotation decision.
_MAX_EXTRA_TTL_S = 1800.0


class UsageCache:
    """In-memory read cache keyed by seat directory (str). Honors `poll_ttl` (default 90s,
    DESIGN.md §4) and any 429 `Retry-After`. Never persisted to disk — usage readings are not
    part of state.json's schema (only the derived cooldownUntil/lastPercent are, in
    cooldown.py) — a fresh process starts with a cold cache and simply re-fetches.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}

    def poll(
        self,
        seat_dir: Path,
        ttl: float = 90.0,
        *,
        force: bool = False,
        timeout: float = DEFAULT_TIMEOUT_S,
        now: float | None = None,
    ) -> UsageSnapshot:
        """Return a usage reading for `seat_dir`, using the cache if it is still fresh.
        On RateLimited, fall back to the cached value if one exists (extending its effective
        TTL by the server's Retry-After) rather than raising — callers that truly have no
        cached value yet will still see RateLimited propagate.
        """
        key = str(seat_dir)
        now = time.time() if now is None else now
        entry = self._entries.get(key)
        if not force and entry is not None:
            effective_ttl = ttl + entry.extra_ttl_s
            if now - entry.cached_at < effective_ttl:
                return entry.snapshot

        try:
            snapshot = fetch_usage(seat_dir, timeout=timeout)
        except RateLimited as exc:
            if entry is not None:
                entry.extra_ttl_s = min(entry.extra_ttl_s + (exc.retry_after_s or ttl), _MAX_EXTRA_TTL_S)
                return entry.snapshot
            raise

        self._entries[key] = _CacheEntry(snapshot=snapshot, cached_at=now)
        return snapshot

    def get_cached(self, seat_dir: Path) -> UsageSnapshot | None:
        entry = self._entries.get(str(seat_dir))
        return entry.snapshot if entry else None
