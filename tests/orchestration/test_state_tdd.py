"""Functional TDD tests for the execution State pattern."""

import unittest

from taip.common.contracts import ExecutionStatus
from taip.orchestration.states import (
    PendingState,
    RunningState,
    CompletedState,
    FailedState,
    PartialState,
)


class FakeExecutionContext:
    """Minimal context for testing state transitions."""

    def __init__(self, state):
        self.current_state = state

    def transition_to(self, state):
        self.current_state = state


class ExecutionStateTDDTests(unittest.TestCase):

    def test_states_expose_correct_status(self):
        cases = [
            (PendingState, ExecutionStatus.PENDING),
            (RunningState, ExecutionStatus.RUNNING),
            (CompletedState, ExecutionStatus.COMPLETED),
            (FailedState, ExecutionStatus.FAILED),
            (PartialState, ExecutionStatus.PARTIAL),
        ]

        for state_type, expected in cases:
            with self.subTest(state=state_type.__name__):
                self.assertEqual(state_type().status, expected)

    def test_pending_can_start(self):
        context = FakeExecutionContext(PendingState())

        context.current_state.start(context)

        self.assertIsInstance(
            context.current_state, RunningState
        )

    def test_pending_can_fail(self):
        context = FakeExecutionContext(PendingState())

        context.current_state.fail(context)

        self.assertIsInstance(
            context.current_state, FailedState
        )

    def test_pending_cannot_complete(self):
        context = FakeExecutionContext(PendingState())

        with self.assertRaises(ValueError):
            context.current_state.complete(context)

    def test_running_can_complete(self):
        context = FakeExecutionContext(RunningState())

        context.current_state.complete(context)

        self.assertIsInstance(
            context.current_state, CompletedState
        )

    def test_running_can_fail(self):
        context = FakeExecutionContext(RunningState())

        context.current_state.fail(context)

        self.assertIsInstance(
            context.current_state, FailedState
        )

    def test_running_cannot_start_again(self):
        context = FakeExecutionContext(RunningState())

        with self.assertRaises(ValueError):
            context.current_state.start(context)

    def test_completed_is_terminal(self):
        context = FakeExecutionContext(CompletedState())

        for action in ("start", "complete", "fail"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    getattr(context.current_state, action)(context)

    def test_failed_is_terminal(self):
        context = FakeExecutionContext(FailedState())

        for action in ("start", "complete", "fail"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    getattr(context.current_state, action)(context)

    def test_partial_can_complete(self):
        context = FakeExecutionContext(PartialState())

        context.current_state.complete(context)

        self.assertIsInstance(
            context.current_state, CompletedState
        )

    def test_partial_can_fail(self):
        context = FakeExecutionContext(PartialState())

        context.current_state.fail(context)

        self.assertIsInstance(
            context.current_state, FailedState
        )

    def test_partial_cannot_restart(self):
        context = FakeExecutionContext(PartialState())

        with self.assertRaises(ValueError):
            context.current_state.start(context)


if __name__ == "__main__":
    unittest.main()