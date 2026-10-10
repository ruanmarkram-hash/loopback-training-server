"""Versioned, conservative activity-observation semantics, without I/O.

An exhausted device query is source enumeration, never all-athlete coverage or
physiological clearance. Exact scan times are audit data, not recurring job keys.
"""
import hashlib
import json
import math
from datetime import UTC, datetime

OBSERVATION_VERSION = 1
FRESHNESS_SECONDS = 900  # Engineering safeguard; not clinical confidence.
MAX_MEMBERS = 20_000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False).encode()).hexdigest()


def instant(value):
    at = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Observation timestamps must be timezone aware")
    return at.astimezone(UTC)


def query_exhaustion(method, status, count):
    if status != "succeeded" or method not in {"sample_no_limit", "sample_capped200"}:
        return "unknown"
    return "truncated" if method == "sample_capped200" and count >= 200 else "exhausted"


def observation_projection(receipt, now):
    if receipt is None:
        return dict(schemaVersion=OBSERVATION_VERSION, status="legacy_unknown", freshness="unknown",
                    sourceCoverage="unknown", trainingCoverage="unknown")
    finalized = receipt.get("finalizedAt")
    freshness = "unknown"
    if finalized:
        age = (instant(now)-instant(finalized)).total_seconds()
        freshness = "fresh" if 0 <= age <= FRESHNESS_SECONDS else "stale" if age > 0 else "invalid_clock"
    ready = (receipt.get("status") == "finalized" and receipt.get("queryStatus") == "succeeded"
             and receipt.get("exhaustion") == "exhausted" and freshness == "fresh"
             and receipt.get("pendingCount") == 0 and receipt.get("failedCount") == 0
             and receipt.get("withdrawnCount") == 0 and receipt.get("invalidatedCount", 0) == 0
             and receipt.get("acknowledgedCount", 0)+receipt.get("duplicateCount", 0) == receipt.get("observedCount"))
    empty = receipt.get("status") == "finalized" and receipt.get("queryStatus") == "succeeded" and receipt.get("observedCount") == 0
    return dict(schemaVersion=OBSERVATION_VERSION, source=receipt["source"], status=receipt["status"],
                queryStatus=receipt.get("queryStatus", "pending"), queryMethod=receipt["queryMethod"],
                exhaustion=receipt.get("exhaustion", "unknown"), freshness=freshness,
                rangeStart=receipt["rangeStart"], rangeEnd=receipt["rangeEnd"],
                observedCount=receipt.get("observedCount"), acknowledgedCount=receipt.get("acknowledgedCount", 0),
                pendingCount=receipt.get("pendingCount", 0), failedCount=receipt.get("failedCount", 0),
                duplicateCount=receipt.get("duplicateCount", 0), invalidatedCount=receipt.get("invalidatedCount", 0), withdrawnCount=receipt.get("withdrawnCount", 0),
                finalizedAt=finalized, lastSuccessfulReconciliationAt=receipt.get("lastSuccessfulReconciliationAt"),
                sourceCoverage="read_access_unknown" if empty else "within_window_observed" if ready else "unknown",
                trainingCoverage="unknown")


def semantic_observation(receipt, now, scheduled_instants=()):
    projection = observation_projection(receipt, now)
    # Exact timestamps remain in the public aggregate for audit but are excluded
    # from the semantic key. Only covered prescribed instants in the review
    # horizon change the effective source window.
    covered = []
    if projection["sourceCoverage"] == "within_window_observed":
        start, end = instant(receipt["rangeStart"]), instant(receipt["rangeEnd"])
        covered = sorted({instant(at).isoformat() for at in scheduled_instants if start <= instant(at) <= end})
    names = ("schemaVersion", "status", "queryStatus", "queryMethod", "exhaustion", "freshness",
             "sourceCoverage", "trainingCoverage", "observedCount", "acknowledgedCount", "pendingCount",
             "failedCount", "duplicateCount", "withdrawnCount", "invalidatedCount")
    return {**{name: projection.get(name) for name in names}, "coveredScheduledInstants": covered,
            "contentDigest": receipt.get("contentDigest") if receipt else None}


def canonical_workout(value):
    """Canonical stored workout content, distinct from transport receipt metadata.

    Sample ordering/values stay intact. Unknown payload fields are evidence, not
    discarded. Only explicit producer extraction timestamps are volatile.
    """
    fields = ("activity_type", "start_date", "end_date", "duration", "total_distance", "total_energy_burned",
              "source", "plan_workout_id", "effort_score", "estimated_effort_score", "data")
    def wire(item):
        if isinstance(item, datetime):
            # Legacy writes allowed naive instants; retain existing UTC interpretation.
            return item.replace(tzinfo=UTC).isoformat() if item.tzinfo is None else item.astimezone(UTC).isoformat()
        if isinstance(item, dict):
            return {k: wire(v) for k, v in item.items() if k not in {"extractedAt", "extractionDate", "scanAt"}}
        if isinstance(item, (list, tuple)):
            return [wire(v) for v in item]
        if item is None or isinstance(item, (str, bool, int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Workout evidence must be finite")
            return item
        # UUID and Decimal become stable JSON values; numeric effort matches wire float.
        from decimal import Decimal
        return float(item) if isinstance(item, Decimal) else str(item)
    numeric_fields = {"duration", "total_distance", "total_energy_burned", "effort_score", "estimated_effort_score"}
    result = {}
    for name in fields:
        item = getattr(value, name, None)
        if name in numeric_fields and item is not None:
            item = float(item)  # WorkoutCreate's typed numeric coercion, including integer JSON inputs.
        result[name] = wire(item)
    return result


def semantic_inventory(value):
    """Terminal inventory digest, independent of client transport encoding."""
    fields = ("sourceId", "disposition", "aliasOf", "explicitSourceDeletion", "serverPayloadDigest", "revision")
    # Client digest binds the incoming/retry transport, not the normalized
    # persisted evidence. Only server-derived identity can change this key.
    normalized = [{name: member.get(name) for name in fields} for member in value]
    return digest(sorted(normalized, key=lambda member: member["sourceId"]))
