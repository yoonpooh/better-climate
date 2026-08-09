"""Pure cooling and heating control logic."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import floor, isfinite


@dataclass(frozen=True)
class ControlResult:
    """Result of a control calculation."""

    cooling_required: bool
    command_temperature: float


@dataclass(frozen=True)
class HeatingControlResult:
    """Result of a heating control calculation."""

    heating_required: bool
    command_temperature: float


def evaluate_source_target_change(
    *,
    old_target: float | None,
    new_target: float,
    expected_targets: Sequence[float],
    step: float,
) -> tuple[bool, int | None]:
    """Return whether to adopt a source target and any matched command index."""
    if step <= 0:
        raise ValueError("step must be greater than zero")
    for index, expected_target in enumerate(expected_targets):
        if abs(new_target - expected_target) < step / 2:
            return False, index
    return (
        old_target is None or abs(new_target - old_target) >= step / 2,
        None,
    )


def round_and_clamp(
    value: float,
    *,
    minimum: float,
    maximum: float,
    step: float,
) -> float:
    if not all(isfinite(item) for item in (value, minimum, maximum, step)):
        raise ValueError("temperature values must be finite")
    if step <= 0 or minimum > maximum:
        raise ValueError("invalid temperature range or step")
    value = floor(value / step + 0.5) * step
    return round(max(minimum, min(maximum, value)), 3)


def calculate_control(
    *,
    room_temperature: float,
    target_temperature: float,
    internal_temperature: float,
    cooling_required: bool,
    hysteresis: float,
    force_offset: float,
    minimum: float,
    maximum: float,
    step: float,
) -> ControlResult:
    """Calculate whether to force cooling and the underlying setpoint."""
    if cooling_required:
        cooling_required = room_temperature > target_temperature
    else:
        cooling_required = room_temperature >= target_temperature + hysteresis

    command = max(target_temperature, internal_temperature) + force_offset
    if cooling_required:
        command = target_temperature
        if internal_temperature <= target_temperature:
            command = internal_temperature - force_offset

    return ControlResult(
        cooling_required,
        round_and_clamp(
            command,
            minimum=minimum,
            maximum=maximum,
            step=step,
        ),
    )


def calculate_heating_control(
    *,
    room_temperature: float,
    target_temperature: float,
    internal_temperature: float,
    heating_required: bool,
    hysteresis: float,
    force_offset: float,
    minimum: float,
    maximum: float,
    step: float,
) -> HeatingControlResult:
    """Calculate whether to force heating and the underlying setpoint."""
    if heating_required:
        heating_required = room_temperature < target_temperature
    else:
        heating_required = room_temperature <= target_temperature - hysteresis

    command = min(target_temperature, internal_temperature) - force_offset
    if heating_required:
        command = target_temperature
        if internal_temperature >= target_temperature:
            command = internal_temperature + force_offset

    return HeatingControlResult(
        heating_required=heating_required,
        command_temperature=round_and_clamp(
            command, minimum=minimum, maximum=maximum, step=step
        ),
    )


def select_heat_cool_mode(
    *,
    room_temperature: float,
    target_low: float,
    target_high: float,
    hysteresis: float,
    active_mode: str | None,
    last_active_mode: str,
) -> str:
    """Select a source while retaining the active source inside the range."""
    if active_mode == "cool":
        return "heat" if room_temperature <= target_low - hysteresis else "cool"
    if active_mode == "heat":
        return "cool" if room_temperature >= target_high + hysteresis else "heat"
    if room_temperature >= target_high + hysteresis:
        return "cool"
    if room_temperature <= target_low - hysteresis:
        return "heat"
    return last_active_mode
