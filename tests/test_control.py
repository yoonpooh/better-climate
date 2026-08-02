"""Tests for Better Climate cooling control logic."""

import importlib.util
import sys
import unittest
from pathlib import Path

CONTROL_PATH = (
    Path(__file__).parents[1] / "custom_components" / "better_climate" / "control.py"
)
SPEC = importlib.util.spec_from_file_location("better_climate_control", CONTROL_PATH)
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)
calculate_control = control.calculate_control
calculate_heating_control = control.calculate_heating_control
evaluate_source_target_change = control.evaluate_source_target_change
round_and_clamp = control.round_and_clamp
select_heat_cool_mode = control.select_heat_cool_mode


class ControlTest(unittest.TestCase):
    """Verify the two-state cooling controller."""

    def test_hysteresis_and_forced_setpoint(self) -> None:
        result = calculate_control(
            room_temperature=24.4,
            target_temperature=24.0,
            internal_temperature=23.0,
            cooling_required=False,
            hysteresis=0.3,
            force_offset=0.5,
            minimum=18,
            maximum=30,
            step=0.5,
        )
        self.assertTrue(result.cooling_required)
        self.assertEqual(result.command_temperature, 22.5)

        result = calculate_control(
            room_temperature=24.0,
            target_temperature=24.0,
            internal_temperature=23.0,
            cooling_required=True,
            hysteresis=0.3,
            force_offset=0.5,
            minimum=18,
            maximum=30,
            step=0.5,
        )
        self.assertFalse(result.cooling_required)
        self.assertEqual(result.command_temperature, 24.0)

    def test_holds_state_inside_hysteresis_band(self) -> None:
        result = calculate_control(
            room_temperature=24.2,
            target_temperature=24.0,
            internal_temperature=24.0,
            cooling_required=False,
            hysteresis=0.3,
            force_offset=0.5,
            minimum=18,
            maximum=30,
            step=0.5,
        )
        self.assertFalse(result.cooling_required)
        self.assertEqual(result.command_temperature, 24.5)

    def test_forces_cooling_off_when_internal_sensor_is_high(self) -> None:
        result = calculate_control(
            room_temperature=24.0,
            target_temperature=24.0,
            internal_temperature=26.0,
            cooling_required=True,
            hysteresis=0.3,
            force_offset=0.5,
            minimum=18,
            maximum=30,
            step=0.5,
        )
        self.assertFalse(result.cooling_required)
        self.assertEqual(result.command_temperature, 26.5)

    def test_rounds_half_up_and_clamps_to_device_limits(self) -> None:
        result = calculate_control(
            room_temperature=30,
            target_temperature=24,
            internal_temperature=18,
            cooling_required=False,
            hysteresis=0.3,
            force_offset=0.75,
            minimum=18,
            maximum=30,
            step=0.5,
        )
        self.assertEqual(result.command_temperature, 18)

        result = calculate_control(
            room_temperature=30,
            target_temperature=24,
            internal_temperature=26,
            cooling_required=False,
            hysteresis=0.3,
            force_offset=0.5,
            minimum=18,
            maximum=30,
            step=0.5,
        )
        self.assertEqual(result.command_temperature, 24)

    def test_rejects_invalid_temperature_step(self) -> None:
        with self.assertRaises(ValueError):
            calculate_control(
                room_temperature=24,
                target_temperature=24,
                internal_temperature=24,
                cooling_required=False,
                hysteresis=0.3,
                force_offset=0.5,
                minimum=18,
                maximum=30,
                step=0,
            )


class HeatingControlTest(unittest.TestCase):
    """Verify external-sensor heating control."""

    def test_hysteresis_and_forced_setpoint(self) -> None:
        result = calculate_heating_control(
            room_temperature=23.7,
            target_temperature=24.0,
            internal_temperature=25.0,
            heating_required=False,
            hysteresis=0.3,
            force_offset=0.5,
            minimum=5,
            maximum=40,
            step=0.5,
        )
        self.assertTrue(result.heating_required)
        self.assertEqual(result.command_temperature, 25.5)

        result = calculate_heating_control(
            room_temperature=24.0,
            target_temperature=24.0,
            internal_temperature=23.0,
            heating_required=True,
            hysteresis=0.3,
            force_offset=0.5,
            minimum=5,
            maximum=40,
            step=0.5,
        )
        self.assertFalse(result.heating_required)
        self.assertEqual(result.command_temperature, 22.5)

    def test_holds_state_inside_hysteresis_band(self) -> None:
        result = calculate_heating_control(
            room_temperature=23.8,
            target_temperature=24.0,
            internal_temperature=24.0,
            heating_required=False,
            hysteresis=0.3,
            force_offset=0.5,
            minimum=5,
            maximum=40,
            step=0.5,
        )
        self.assertFalse(result.heating_required)
        self.assertEqual(result.command_temperature, 23.5)

    def test_rounds_and_clamps_to_boiler_limits(self) -> None:
        result = calculate_heating_control(
            room_temperature=10,
            target_temperature=24,
            internal_temperature=40,
            heating_required=False,
            hysteresis=0.3,
            force_offset=0.75,
            minimum=5,
            maximum=40,
            step=0.5,
        )
        self.assertTrue(result.heating_required)
        self.assertEqual(result.command_temperature, 40)

    def test_rejects_invalid_temperature_step(self) -> None:
        with self.assertRaises(ValueError):
            calculate_heating_control(
                room_temperature=24,
                target_temperature=24,
                internal_temperature=24,
                heating_required=False,
                hysteresis=0.3,
                force_offset=0.5,
                minimum=5,
                maximum=40,
                step=0,
            )


class SourceSynchronizationTest(unittest.TestCase):
    """Verify original-source target synchronization."""

    def test_ignores_the_wrappers_own_command(self) -> None:
        self.assertEqual(
            evaluate_source_target_change(
                old_target=24.0,
                new_target=22.5,
                expected_targets=[23.0, 22.5],
                step=0.5,
            ),
            (False, 1),
        )

    def test_adopts_a_manual_source_change(self) -> None:
        self.assertEqual(
            evaluate_source_target_change(
                old_target=22.5,
                new_target=25.0,
                expected_targets=[23.0],
                step=0.5,
            ),
            (True, None),
        )

    def test_unrelated_event_does_not_consume_expected_command(self) -> None:
        self.assertEqual(
            evaluate_source_target_change(
                old_target=24.0,
                new_target=24.0,
                expected_targets=[22.5],
                step=0.5,
            ),
            (False, None),
        )

    def test_ignores_sub_step_noise(self) -> None:
        self.assertEqual(
            evaluate_source_target_change(
                old_target=24.0,
                new_target=24.1,
                expected_targets=[],
                step=0.5,
            ),
            (False, None),
        )

    def test_rejects_invalid_temperature_step(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_source_target_change(
                old_target=24,
                new_target=25,
                expected_targets=[],
                step=0,
            )


class TargetNormalizationTest(unittest.TestCase):
    """Verify virtual and fallback target normalization."""

    def test_rounds_half_up_and_clamps(self) -> None:
        self.assertEqual(
            round_and_clamp(24.25, minimum=18, maximum=30, step=0.5),
            24.5,
        )
        self.assertEqual(
            round_and_clamp(50, minimum=18, maximum=30, step=0.5),
            30,
        )

    def test_rejects_invalid_temperature_step(self) -> None:
        with self.assertRaises(ValueError):
            round_and_clamp(24, minimum=18, maximum=30, step=0)

    def test_rejects_non_finite_or_inverted_ranges(self) -> None:
        with self.assertRaises(ValueError):
            round_and_clamp(float("nan"), minimum=18, maximum=30, step=0.5)
        with self.assertRaises(ValueError):
            round_and_clamp(24, minimum=30, maximum=18, step=0.5)


class HeatCoolSelectionTest(unittest.TestCase):
    """Verify heat/cool source retention and boundary switching."""

    def test_retains_active_source_inside_range(self) -> None:
        self.assertEqual(
            select_heat_cool_mode(
                room_temperature=24,
                target_low=23,
                target_high=25,
                hysteresis=0.3,
                active_mode="cool",
                last_active_mode="heat",
            ),
            "cool",
        )
        self.assertEqual(
            select_heat_cool_mode(
                room_temperature=24,
                target_low=23,
                target_high=25,
                hysteresis=0.3,
                active_mode="heat",
                last_active_mode="cool",
            ),
            "heat",
        )

    def test_switches_only_after_opposite_boundary(self) -> None:
        self.assertEqual(
            select_heat_cool_mode(
                room_temperature=22.7,
                target_low=23,
                target_high=25,
                hysteresis=0.3,
                active_mode="cool",
                last_active_mode="cool",
            ),
            "heat",
        )
        self.assertEqual(
            select_heat_cool_mode(
                room_temperature=25.3,
                target_low=23,
                target_high=25,
                hysteresis=0.3,
                active_mode="heat",
                last_active_mode="heat",
            ),
            "cool",
        )


if __name__ == "__main__":
    unittest.main()
