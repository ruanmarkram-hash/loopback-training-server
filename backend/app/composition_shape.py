"""Supported composition structure, without coercing or dropping malformed parts."""

import sys


def finite_nonnegative(value):
    """A finite quantity representable by the numeric consumers, without coercion."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= sys.float_info.max


def composition_shape_valid(composition):
    if composition is None:
        return True
    if not isinstance(composition, dict):
        return False

    def positive(value):
        return finite_nonnegative(value) and value > 0

    def goal(value):
        if not isinstance(value, dict) or any(
            key in value and not isinstance(value[key], str) for key in ("type", "unit")
        ):
            return False
        return value.get("type") not in ("distance", "time") or positive(value.get("value"))

    def alert(value):
        if not isinstance(value, dict) or any(
            key in value and not isinstance(value[key], str) for key in ("type", "unit")
        ):
            return False
        return value.get("type") not in ("speed", "pace") or (
            positive(value.get("min")) and positive(value.get("max")) and value["min"] <= value["max"]
        )

    def step(value):
        return (
            isinstance(value, dict)
            and ("purpose" not in value or isinstance(value["purpose"], str))
            and ("goal" not in value or goal(value["goal"]))
            and ("alert" not in value or alert(value["alert"]))
        )

    for key in ("warmup", "cooldown"):
        if key in composition and not step(composition[key]):
            return False
    if "singleGoal" in composition and not goal(composition["singleGoal"]):
        return False
    if "blocks" in composition:
        blocks = composition["blocks"]
        if not isinstance(blocks, list) or len(blocks) > 200:
            return False
        for block in blocks:
            if not isinstance(block, dict):
                return False
            repetitions = block.get("iterations", 1)
            if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
                return False
            steps = block.get("steps", [])
            if not isinstance(steps, list) or len(steps) > 200 or any(not step(value) for value in steps):
                return False
    return True
