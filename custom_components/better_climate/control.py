"""Pure cooling and heating control logic."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import floor, isfinite

_QUANTIZE_TOLERANCE = 1e-3


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


def _coarse_grids(step: float) -> list[float]:
    """Return grids coarser than ``step``, including whole degrees."""
    grids: list[float] = []
    coarse = step * 2
    while coarse < 1 - 1e-9:
        grids.append(coarse)
        coarse *= 2
    if step < 1 - 1e-9:
        grids.append(1.0)
    return grids


def _grid_values(commanded: float, coarse: float) -> tuple[float, float]:
    """Return the floor and half-up snaps of a command onto ``coarse``."""
    floor_value = round(floor(commanded / coarse + 1e-9) * coarse, 6)
    up_value = round(floor(commanded / coarse + 0.5) * coarse, 6)
    return floor_value, up_value


def matches_coarse_command(
    reported: float,
    commanded: float,
    step: float,
    *,
    quantizer: str | None = None,
) -> bool:
    """Return whether ``reported`` is ``commanded`` snapped onto a coarser grid.

    ``quantizer`` restricts the snap to ``floor`` or ``up`` after a source has
    shown which way it resolves half steps. ``None`` accepts either snap so the
    first coarse reading can be recognized.
    """
    if step <= 0:
        raise ValueError("step must be greater than zero")
    if abs(reported - commanded) < step / 2:
        return False
    for coarse in _coarse_grids(step):
        floor_value, up_value = _grid_values(commanded, coarse)
        distinct = abs(floor_value - up_value) > _QUANTIZE_TOLERANCE
        if not distinct:
            continue
        if quantizer == "floor" and abs(reported - floor_value) <= _QUANTIZE_TOLERANCE:
            return True
        if quantizer == "up" and abs(reported - up_value) <= _QUANTIZE_TOLERANCE:
            return True
        if quantizer is None and (
            abs(reported - floor_value) <= _QUANTIZE_TOLERANCE
            or abs(reported - up_value) <= _QUANTIZE_TOLERANCE
        ):
            return True
    return False


def learn_quantizer(reported: float, commanded: float, step: float) -> str | None:
    """Return ``floor`` or ``up`` when a coarse reading identifies one of them."""
    if step <= 0:
        raise ValueError("step must be greater than zero")
    if abs(reported - commanded) < step / 2:
        return None
    found: list[str] = []
    for coarse in _coarse_grids(step):
        floor_value, up_value = _grid_values(commanded, coarse)
        if abs(floor_value - up_value) <= _QUANTIZE_TOLERANCE:
            continue
        if abs(reported - floor_value) <= _QUANTIZE_TOLERANCE:
            found.append("floor")
        elif abs(reported - up_value) <= _QUANTIZE_TOLERANCE:
            found.append("up")
    unique = set(found)
    if len(unique) == 1:
        return found[0]
    return None


def has_finer_resolution(value: float, step: float) -> bool:
    """Return whether ``value`` is on ``step`` but not on a whole degree."""
    if step <= 0:
        raise ValueError("step must be greater than zero")
    if abs(value - round(value)) <= _QUANTIZE_TOLERANCE:
        return False
    return abs(value / step - round(value / step)) <= _QUANTIZE_TOLERANCE


def source_reported_command(
    reported: float,
    commanded: float,
    step: float,
    *,
    allow_coarser: bool = False,
    quantizer: str | None = None,
) -> bool:
    """Return whether a source reading already represents ``commanded``."""
    if step <= 0:
        raise ValueError("step must be greater than zero")
    if abs(reported - commanded) < step / 2:
        return True
    if not allow_coarser:
        return False
    return matches_coarse_command(reported, commanded, step, quantizer=quantizer)


def evaluate_source_target_change(
    *,
    old_target: float | None,
    new_target: float,
    expected_targets: Sequence[float],
    step: float,
    coarser_targets: Sequence[float] = (),
    quantizer: str | None = None,
) -> tuple[bool, int | None]:
    """Return whether to adopt a source target and any matched command index."""
    if step <= 0:
        raise ValueError("step must be greater than zero")
    for index, expected_target in enumerate(expected_targets):
        if abs(new_target - expected_target) < step / 2:
            return False, index
    for expected_target in coarser_targets:
        if matches_coarse_command(
            new_target, expected_target, step, quantizer=quantizer
        ):
            return False, None
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
