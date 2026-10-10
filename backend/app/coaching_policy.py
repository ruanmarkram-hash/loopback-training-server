"""Provisional engineering policy, not clinical or proprietary coaching thresholds."""

from datetime import UTC, datetime, timedelta

POLICY = dict(
    version="loopback-conservative-v1",
    minimum_independent_sessions=3,
    lookback_days=28,
    pace_error_band=0.03,
    maximum_variability=0.10,
    minimum_coverage=0.85,
    volume_window_weeks=4,
    partial_week="exclude",
    missed_session_trigger=3,
    elapsed_gap_days=10,
    cooldown_hours=72,
    maximum_change_fraction=0.05,
    freeze_hours=24,
    lease_seconds=180,
    maximum_attempts=3,
    proposal_expiry_hours=72,
)


def finite_positive(value):
    from app.composition_shape import finite_nonnegative

    return finite_nonnegative(value) and value > 0


def speed_meters_per_second(alert):
    if alert.get("type") != "speed":
        return None
    unit = alert.get("unit", "metersPerSecond")
    factor = {"metersPerSecond": 1, "kilometersPerHour": 1 / 3.6}.get(unit)
    if factor is None or not all(finite_positive(alert.get(k)) for k in ("min", "max")) or alert["min"] > alert["max"]:
        return None
    bounds = alert["min"] * factor, alert["max"] * factor
    if not all(finite_positive(value) for value in bounds):
        return None
    return bounds


def canonical_metrics(workouts, queues, now=None, zone="Australia/Brisbane"):
    from zoneinfo import ZoneInfo

    from app.plan_validation import session_from_composition

    now = now or datetime.now(UTC)
    tz = ZoneInfo(zone)
    start = now - timedelta(days=28)
    runs = [w for w in workouts if w.activity_type == "running" and start <= w.start_date <= now]
    distance = sum(w.total_distance or 0 for w in runs)
    duration = sum(w.duration or 0 for w in runs)
    weeks = {}

    def week_row(day):
        monday = day - timedelta(days=day.weekday())
        key = monday.isoformat()
        return weeks.setdefault(
            key,
            dict(
                distance_meters=0,
                duration_seconds=0,
                session_count=0,
                hard_session_count=0,
                hard_session_evidence="reported_effort_only",
                longest_run_meters=0,
                longest_run_share=None,
                planned_distance_meters=0,
                planned_duration_seconds=0,
                planned_session_count=0,
                planned_estimated=False,
                unknown_planned_sessions=0,
                partial_week=monday + timedelta(days=7) > now.astimezone(tz).date()
                or monday < start.astimezone(tz).date(),
                unknown_distance_sessions=0,
                unknown_duration_sessions=0,
            ),
        )

    for w in runs:
        x = week_row(w.start_date.astimezone(tz).date())
        x["distance_meters"] += w.total_distance or 0
        x["duration_seconds"] += w.duration or 0
        x["session_count"] += 1
        x["unknown_distance_sessions"] += w.total_distance is None
        x["unknown_duration_sessions"] += w.duration is None
        x["hard_session_count"] += w.effort_score is not None and w.effort_score >= 7
        x["longest_run_meters"] = max(x["longest_run_meters"], w.total_distance or 0)
    for q in queues:
        if (
            q.activity_type != "running"
            or not q.scheduled_date
            or not start <= q.scheduled_date <= now + timedelta(days=14)
        ):
            continue
        day = q.scheduled_date.astimezone(tz).date()
        x = week_row(day)
        planned = session_from_composition(q.workout_data, day, q.title, None)
        x["planned_distance_meters"] += planned.distance_m
        x["planned_duration_seconds"] += planned.duration_s
        x["planned_session_count"] += 1
        x["planned_estimated"] |= planned.estimated
        x["unknown_planned_sessions"] += not planned.has_data
    for x in weeks.values():
        if x["unknown_planned_sessions"]:
            x["planned_distance_meters"] = None
            x["planned_duration_seconds"] = None
        x["longest_run_share"] = x["longest_run_meters"] / x["distance_meters"] if x["distance_meters"] else None
        x["actual_vs_planned_distance_ratio"] = (
            x["distance_meters"] / x["planned_distance_meters"]
            if x["planned_distance_meters"]
            and not x["partial_week"]
            and not x["unknown_distance_sessions"]
            and not x["planned_estimated"]
            else None
        )
        x["comparison_status"] = "partial_week_not_compared" if x["partial_week"] else "recorded_only_coverage_unknown"
    return dict(
        actual_distance_meters=distance,
        actual_duration_seconds=duration,
        session_count=len(runs),
        weeks=weeks,
        timezone=zone,
        planned_future_session_count=sum(
            q.status not in ("completed", "skipped") and q.scheduled_date is not None and q.scheduled_date > now
            for q in queues
        ),
    )


def deterministic_review(workouts, assessments, feedback, queues, now=None, zone="Australia/Brisbane"):
    from zoneinfo import ZoneInfo

    now = now or datetime.now(UTC)
    tz = ZoneInfo(zone)
    ids = {
        w.id: w
        for w in workouts
        if w.activity_type == "running" and now - timedelta(days=POLICY["lookback_days"]) <= w.start_date <= now
    }
    days = {}
    for a in assessments:
        w = ids.get(a.workout_id)
        if w and a.data.get("pace_evidence") == "eligible":
            days[w.start_date.astimezone(tz).date()] = a.data
    results = [d.get("execution") for d in days.values()]
    status = "monitoring"
    direction = None
    if len(results) >= POLICY["minimum_independent_sessions"]:
        if all(x == "over_target" for x in results):
            status = "improvement_supported"
            direction = "faster"
        elif all(x == "under_target" for x in results):
            status = "reduction_supported"
            direction = "slower"
        elif all(x == "within_targets" for x in results):
            status = "on_track"
        else:
            status = "mixed_evidence"
    elif not results:
        status = "excluded"
    metrics = canonical_metrics(workouts, queues, now, zone)
    full = [
        x
        for x in metrics["weeks"].values()
        if not x["partial_week"] and x["session_count"] > 0 and not x["unknown_distance_sessions"]
    ]
    if full:
        average = sum(x["distance_meters"] for x in full) / len(full)
        current = next((x for x in metrics["weeks"].values() if x["partial_week"] and x["session_count"]), None)
        if current and current["distance_meters"] > average * 1.3 and direction == "faster":
            status = "mixed_evidence"
            direction = None
    from app.coaching_feedback import eligible_skips
    misses = eligible_skips(feedback, queues, now, POLICY["lookback_days"])
    last = max((w.start_date for w in workouts if w.activity_type == "running" and w.start_date <= now), default=None)
    gap = (now - last).days if last else None
    return dict(
        paceStatus=status,
        paceDirection=direction,
        independentEligibleDays=len(days),
        minimumIndependentDays=POLICY["minimum_independent_sessions"],
        paceReason="Independent original-prescription quality evidence required; easy, partial, unmatched and unknown-version runs excluded",
        explicitMissCount=len(misses),
        interruptionReview=len(misses) >= POLICY["missed_session_trigger"]
        or gap is not None
        and gap >= POLICY["elapsed_gap_days"],
        elapsedGapDays=gap,
        gapCoverage="unknown",
        interruptionPolicy="Do not cram missed sessions or change fixed event date; bounded future reduction or unchanged plan requires review",
        volumeStatus="recorded_load_monitoring",
        partialWeekTreatment=POLICY["partial_week"],
        metrics=metrics,
    )


def assess(workout, prescription):
    """Never infer structure or prescription version from temporal proximity."""
    result = dict(
        match="unmatched",
        structure="unknown",
        pace_evidence="ineligible",
        execution="not_assessable",
        reason="Original prescription not established",
        prescription_revision=None,
    )
    if getattr(workout, "source_withdrawn", False):
        result["reason"] = "Source-reported withdrawal; recorded load retained with uncertainty"
        return result
    if not prescription:
        return result
    result["match"] = "exact_identifier"
    result["prescription_revision"] = str(prescription.id)
    # Exact logical linkage is not proof of the version actually executed.
    observed = (workout.data or {}).get("prescriptionRevision")
    if observed != str(prescription.id):
        result["reason"] = "Logical workout linked, executed prescription version unknown"
        return result
    issued = getattr(prescription, "created_at", None)
    started = getattr(workout, "start_date", None)
    if issued is not None and started is not None and (issued.tzinfo is None or started.tzinfo is None):
        result["reason"] = "Original execution timestamp timezone unknown"
        return result
    if issued is None or started is None or issued > started:
        result["reason"] = "Original prescription issuance unknown or after activity began"
        return result
    segments = (workout.data or {}).get("prescribedSegments")
    if not isinstance(segments, list) or not segments:
        result["reason"] = "Measured work segments unavailable"
        return result
    composition = prescription.snapshot.get("composition") or {}
    from app.composition_shape import composition_shape_valid

    if not composition_shape_valid(composition):
        result["reason"] = "Original prescription structure is malformed; execution remains unknown"
        return result
    ordered = []
    if composition.get("warmup"):
        ordered.append(composition["warmup"])
    for block in composition.get("blocks", []):
        repeats = block.get("iterations", 1)
        if not isinstance(repeats, int) or not 1 <= repeats <= 100:
            result["reason"] = "Original repetition structure invalid"
            return result
        for _ in range(repeats):
            ordered.extend(block.get("steps", []))
    if composition.get("cooldown"):
        ordered.append(composition["cooldown"])
    if not ordered:
        result["reason"] = "Original ordered structure unavailable"
        return result
    measured = {}
    for segment in segments:
        if not isinstance(segment, dict):
            result["reason"] = "Malformed measured segment object; execution evidence unavailable"
            return result
        index = segment.get("stepIndex")
        if not isinstance(index, int) or isinstance(index, bool) or index in measured or not 0 <= index < len(ordered):
            result["reason"] = "Ambiguous measured step correspondence"
            return result
        measured[index] = segment
    if len(measured) != len(ordered):
        result.update(structure="partial", reason="Missing prescribed steps or repetitions")
        return result
    eligible = []
    measured_paces = []
    original_pace_bounds = []
    quality_coverages = []
    quality_completions = []
    for index, step in enumerate(ordered):
        segment = measured[index]
        purpose = step.get("purpose")
        if segment.get("purpose") != purpose:
            result["reason"] = "Measured purpose differs from original prescription"
            return result
        distance = segment.get("distanceMeters")
        duration = segment.get("durationSeconds")
        if not finite_positive(duration) or not finite_positive(distance):
            result["reason"] = "Measured step distance/time unavailable"
            return result
        goal = step.get("goal") or {}
        target = goal.get("value")
        if not finite_positive(target):
            result["reason"] = "Original step goal unavailable"
            return result
        if goal.get("type") in ("distance", "time"):
            factors = (
                {"kilometers": 1000, "km": 1000, "meters": 1, "m": 1, "miles": 1609.344, "mi": 1609.344}
                if goal["type"] == "distance"
                else {"seconds": 1, "s": 1, "minutes": 60, "min": 60, "hours": 3600, "h": 3600}
            )
            if goal.get("unit") not in factors:
                result["reason"] = "Original goal unit unsupported; execution remains unknown"
                return result
            target *= factors[goal["unit"]]
            complete = (distance if goal["type"] == "distance" else duration) / target
        else:
            result["reason"] = "Original open goal not comparable"
            return result
        if not finite_positive(complete):
            result["reason"] = "Derived prescribed step completion is not finite"
            return result
        if complete < POLICY["minimum_coverage"]:
            result.update(structure="partial", reason="Prescribed step incomplete")
            return result
        if (
            segment.get("measurementSource") != "healthkit"
            or not finite_positive(segment.get("sampleCoverage"))
            or not POLICY["minimum_coverage"] <= segment["sampleCoverage"] <= 1
        ):
            result["reason"] = "Insufficient measured sample provenance/coverage"
            return result
        if purpose not in ("work", "interval", "tempo"):
            continue
        alert = step.get("alert") or {}
        bounds = speed_meters_per_second(alert)
        if bounds is None:
            continue
        lo = 1000 / bounds[1]
        hi = 1000 / bounds[0]
        pace = duration / distance * 1000
        if not all(finite_positive(value) for value in (lo, hi, pace)):
            result["reason"] = "Derived original or measured pace is not finite"
            return result
        measured_paces.append(pace)
        original_pace_bounds.append((lo, hi))
        quality_coverages.append(segment["sampleCoverage"])
        quality_completions.append(complete)
        eligible.append(
            "over_target"
            if pace < lo * (1 - POLICY["pace_error_band"])
            else "under_target"
            if pace > hi * (1 + POLICY["pace_error_band"])
            else "within_targets"
        )
    result["structure"] = "prescribed_steps_completed"
    if not eligible or composition.get("trainingPurpose") not in ("quality", "intervals", "tempo"):
        result["reason"] = "No explicit original quality purpose and pace target ranges"
        return result
    # Normalize before summing: avoid overflow of the sum and underflow of
    # individually divided subnormal paces. At least one normalized value is 1.
    pace_scale = max(measured_paces)
    variability = ((pace_scale - min(measured_paces)) / pace_scale) / (
        sum(pace / pace_scale for pace in measured_paces) / len(measured_paces)
    )
    if variability > POLICY["maximum_variability"]:
        result.update(
            pace_evidence="ineligible",
            execution="variable",
            reason="Measured quality pace variability exceeds provisional bound",
        )
        return result
    result.update(
        pace_evidence="eligible",
        execution=eligible[0] if len(set(eligible)) == 1 else "variable",
        reason="All measured prescribed steps compared to immutable original target ranges",
        # Bounded derived facts only, after every provenance/structure guard.
        # Original bounds come from the immutable prescription, never activity hints.
        qualityEvidence=dict(
            sessionDate=started.isoformat(),
            prescriptionRevision=str(prescription.id),
            qualityStepCount=len(measured_paces),
            measuredPaceMinSecondsPerKm=min(measured_paces),
            measuredPaceMaxSecondsPerKm=max(measured_paces),
            originalPaceMinSecondsPerKm=min(lo for lo, _ in original_pace_bounds),
            originalPaceMaxSecondsPerKm=max(hi for _, hi in original_pace_bounds),
            minimumSampleCoverage=min(quality_coverages),
            minimumStepCompletion=min(quality_completions),
            paceVariabilityFraction=variability,
        ),
    )
    return result
