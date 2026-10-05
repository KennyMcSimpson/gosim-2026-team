"""Optional, transport-free boundary for selecting a precomputed plan.

The bridge is deliberately smaller than the agent protocol.  A planner may
offer a few already-computed plans to an external decision layer (for example
Pi) without exporting the target catalogue or the full action payload.  The
external layer can return only an opaque choice id.  The caller still owns
action construction and must run :func:`agent_core.validation.validate_action`
before sending a response to the platform.

There is no I/O in this module: it does not print, make network calls, load
credentials, execute code, or update survey progress.  A bridge instance has
one active offer.  Issuing a new offer invalidates the previous one, and a
successful selection consumes the active offer so a response cannot be
replayed.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable, Optional


BRIDGE_VERSION = 1
DEFAULT_MAX_CHOICES = 8
MAX_CHOICES = 64
MAX_TOKEN_LENGTH = 256
MAX_BUDGET_SECONDS = 86_400.0
MAX_SUMMARY_VALUE = 1_000_000_000.0
MAX_DURATION_SECONDS = 86_400
MAX_PROGRAM_LENGTH = 32
MAX_CHOICE_ID_LENGTH = 64

# Only these scalar fields cross the boundary.  In particular, assignments,
# pointings, target ids, and planner internals are intentionally excluded.
_NUMERIC_FIELDS = ("utility", "science", "rate", "reward")
_DURATION_FIELDS = ("duration_seconds", "duration")


class BridgeInputError(ValueError):
    """Raised for invalid arguments supplied by the local planner."""


@dataclass(frozen=True)
class _Choice:
    choice_id: str
    plan: Any
    summary: dict[str, Any]


@dataclass(frozen=True)
class _Offer:
    token: str
    version: int
    expires_at: float
    choices: dict[str, _Choice]


def _is_real_number(value: Any) -> bool:
    """Return true for finite numeric values, excluding booleans."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bounded_float(value: Any, field: str) -> Optional[float]:
    """Normalize one optional summary number or reject the candidate."""
    if not _is_real_number(value):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > MAX_SUMMARY_VALUE:
        return None
    return number


def _summary_for(plan: Any) -> Optional[dict[str, Any]]:
    """Extract a small JSON-safe summary from a planner plan.

    Invalid optional fields reject this candidate instead of being coerced.  A
    plan only needs one recognized field to be offerable; unknown fields are
    ignored and therefore cannot cause catalogue or action serialization.
    """
    if not isinstance(plan, Mapping):
        return None

    summary: dict[str, Any] = {}
    saw_field = False
    for field in _NUMERIC_FIELDS:
        if field not in plan:
            continue
        saw_field = True
        number = _bounded_float(plan[field], field)
        if number is None:
            return None
        summary[field] = number

    duration_field = next((field for field in _DURATION_FIELDS if field in plan), None)
    if duration_field is not None:
        saw_field = True
        raw_duration = plan[duration_field]
        if not _is_real_number(raw_duration):
            return None
        duration = float(raw_duration)
        if (not math.isfinite(duration) or duration < 0 or
                duration > MAX_DURATION_SECONDS or not duration.is_integer()):
            return None
        summary["duration_seconds"] = int(duration)

    if "program" in plan:
        saw_field = True
        program = plan["program"]
        if (not isinstance(program, str) or not program or
                len(program) > MAX_PROGRAM_LENGTH):
            return None
        summary["program"] = program

    return summary if saw_field else None


def _choice_id(summary: Mapping[str, Any], occurrence: int) -> str:
    """Create a stable opaque id without embedding target or action fields."""
    encoded = json.dumps(
        {"summary": dict(summary), "occurrence": occurrence},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:32]
    return f"cb1_{digest}"


class CandidateBridge:
    """Hold one bounded offer and resolve a later choice without transport.

    ``clock`` is injectable solely for deterministic tests.  Production callers
    should use the default monotonic clock and should treat ``None`` from
    :meth:`take` as "let the local deterministic policy decide".
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_choices: int = DEFAULT_MAX_CHOICES,
    ) -> None:
        if not isinstance(max_choices, int) or isinstance(max_choices, bool):
            raise BridgeInputError("max_choices must be an integer")
        if not 1 <= max_choices <= MAX_CHOICES:
            raise BridgeInputError(f"max_choices must be in [1, {MAX_CHOICES}]")
        if not callable(clock):
            raise BridgeInputError("clock must be callable")
        self._clock = clock
        self._max_choices = max_choices
        self._version = 0
        self._offer: Optional[_Offer] = None

    @property
    def active_version(self) -> Optional[int]:
        """Return the current offer version for local diagnostics/tests."""
        return self._offer.version if self._offer is not None else None

    def offer(
        self,
        plans: Iterable[Mapping[str, Any]] | Mapping[str, Any],
        token: str,
        budget_seconds: float,
    ) -> dict[str, Any]:
        """Export bounded summaries for at most ``max_choices`` plans.

        The original plan objects remain private to this instance and are
        returned by :meth:`take` after a valid selection.  Invalid candidates
        are omitted; invalid token/budget arguments raise ``BridgeInputError``
        so the caller can immediately use its deterministic best policy.
        """
        # A new offer always invalidates the old one, including when argument
        # validation fails.  This prevents accidental reuse of stale choices.
        self._offer = None
        checked_token = self._validate_token(token)
        budget = self._validate_budget(budget_seconds)
        try:
            now = float(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise BridgeInputError("clock did not return a number") from exc
        if not math.isfinite(now):
            raise BridgeInputError("clock did not return a finite number")

        if isinstance(plans, Mapping):
            source: Iterable[Any] = (plans,)
        else:
            if plans is None or isinstance(plans, (str, bytes, bytearray)):
                raise BridgeInputError("plans must be an iterable of mappings")
            try:
                source = iter(plans)
            except TypeError as exc:
                raise BridgeInputError("plans must be an iterable of mappings") from exc

        choices: dict[str, _Choice] = {}
        occurrences: dict[str, int] = {}
        # islice keeps an untrusted/infinite iterable from becoming a broad
        # catalogue export.  Invalid entries still consume one bounded slot.
        for plan in itertools.islice(source, self._max_choices):
            summary = _summary_for(plan)
            if summary is None:
                continue
            canonical = json.dumps(summary, sort_keys=True, separators=(",", ":"))
            occurrence = occurrences.get(canonical, 0)
            occurrences[canonical] = occurrence + 1
            choice_id = _choice_id(summary, occurrence)
            # The digest makes collisions extraordinarily unlikely, but do not
            # silently overwrite if a custom hash implementation ever changes.
            while choice_id in choices:
                occurrence += 1
                choice_id = _choice_id(summary, occurrence)
            choices[choice_id] = _Choice(choice_id, plan, summary)

        self._version += 1
        self._offer = _Offer(
            token=checked_token,
            version=self._version,
            expires_at=now + budget,
            choices=choices,
        )
        return {
            "bridge_version": BRIDGE_VERSION,
            "state_token": checked_token,
            "offer_version": self._version,
            "expires_in_seconds": budget,
            "choices": [
                {"choice_id": choice.choice_id, **choice.summary}
                for choice in choices.values()
            ],
        }

    def take(self, response: Any, token: str) -> Any | None:
        """Resolve a valid response to its original plan, or return ``None``.

        Responses are intentionally strict and contain no parameter bag.  Any
        malformed, stale, unknown, expired, or externally parameterized value
        fails closed without raising, leaving deterministic selection to the
        caller.
        """
        offer = self._offer
        if offer is None:
            return None
        try:
            checked_token = self._validate_token(token)
            now = float(self._clock())
        except (BridgeInputError, TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(now):
            self._offer = None
            return None
        if now >= offer.expires_at:
            self._offer = None
            return None
        if checked_token != offer.token:
            return None
        if not isinstance(response, Mapping):
            return None
        if set(response) != {"state_token", "offer_version", "choice_id"}:
            return None
        if response.get("state_token") != checked_token:
            return None
        version = response.get("offer_version")
        choice_id = response.get("choice_id")
        if (not isinstance(version, int) or isinstance(version, bool) or
                version != offer.version):
            return None
        if (not isinstance(choice_id, str) or not choice_id or
                len(choice_id) > MAX_CHOICE_ID_LENGTH):
            return None
        choice = offer.choices.get(choice_id)
        if choice is None:
            return None
        self._offer = None
        return choice.plan

    @staticmethod
    def _validate_token(token: Any) -> str:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
            raise BridgeInputError("token must be a non-empty bounded string")
        return token

    @staticmethod
    def _validate_budget(budget_seconds: Any) -> float:
        if not _is_real_number(budget_seconds):
            raise BridgeInputError("budget_seconds must be numeric")
        budget = float(budget_seconds)
        if (not math.isfinite(budget) or budget <= 0 or
                budget > MAX_BUDGET_SECONDS):
            raise BridgeInputError("budget_seconds is outside the bounded range")
        return budget


__all__ = [
    "BRIDGE_VERSION",
    "BridgeInputError",
    "CandidateBridge",
]
