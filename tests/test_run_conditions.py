import unittest

from trievo_bench.run_conditions import (
    DEFAULT_RUN_CONDITION,
    RunCondition,
    RunConditionError,
    parse_run_condition,
    require_run_condition,
    run_condition_policy,
)


class RunConditionTest(unittest.TestCase):
    def test_default_preserves_frozen_runtime_behavior(self) -> None:
        self.assertIs(DEFAULT_RUN_CONDITION, RunCondition.FROZEN)
        self.assertIs(parse_run_condition(None), RunCondition.FROZEN)

        policy = run_condition_policy(None)
        self.assertTrue(policy.workspace_persists_across_turns)
        self.assertTrue(policy.sut_state_persists_across_turns)
        self.assertFalse(policy.sut_evolution_enabled)
        self.assertFalse(policy.learner_updates_enabled)
        self.assertFalse(policy.harness_updates_enabled)

    def test_reset_policy_reseeds_workspace_and_sut_for_each_turn(self) -> None:
        policy = run_condition_policy("reset")

        self.assertIs(policy.condition, RunCondition.RESET)
        self.assertFalse(policy.workspace_persists_across_turns)
        self.assertTrue(policy.reseed_workspace_before_each_turn)
        self.assertTrue(policy.apply_only_current_turn_transition)
        self.assertFalse(policy.sut_state_persists_across_turns)
        self.assertTrue(policy.recreate_sut_state_before_each_turn)
        self.assertFalse(policy.sut_evolution_enabled)

    def test_frozen_policy_keeps_workspace_but_blocks_sut_evolution(self) -> None:
        policy = RunCondition.FROZEN.policy

        self.assertTrue(policy.workspace_persists_across_turns)
        self.assertFalse(policy.reseed_workspace_before_each_turn)
        self.assertFalse(policy.apply_only_current_turn_transition)
        self.assertTrue(policy.sut_state_persists_across_turns)
        self.assertFalse(policy.recreate_sut_state_before_each_turn)
        self.assertFalse(policy.sut_evolution_enabled)

    def test_evolving_policy_keeps_workspace_and_enables_sut_evolution(self) -> None:
        policy = run_condition_policy(RunCondition.EVOLVING)

        self.assertTrue(policy.workspace_persists_across_turns)
        self.assertFalse(policy.reseed_workspace_before_each_turn)
        self.assertFalse(policy.apply_only_current_turn_transition)
        self.assertTrue(policy.sut_state_persists_across_turns)
        self.assertFalse(policy.recreate_sut_state_before_each_turn)
        self.assertTrue(policy.sut_evolution_enabled)
        self.assertTrue(policy.learner_updates_enabled)
        self.assertTrue(policy.harness_updates_enabled)

    def test_parser_normalizes_text(self) -> None:
        self.assertIs(parse_run_condition(" RESET "), RunCondition.RESET)
        self.assertIs(parse_run_condition("Frozen"), RunCondition.FROZEN)
        self.assertIs(parse_run_condition("evolving"), RunCondition.EVOLVING)

    def test_invalid_condition_is_rejected(self) -> None:
        with self.assertRaisesRegex(RunConditionError, "unknown run condition"):
            parse_run_condition("training")

        with self.assertRaisesRegex(RunConditionError, "must be one of"):
            parse_run_condition(123)  # type: ignore[arg-type]

    def test_required_parser_rejects_missing_condition(self) -> None:
        with self.assertRaisesRegex(RunConditionError, "required"):
            require_run_condition(None)


if __name__ == "__main__":
    unittest.main()
