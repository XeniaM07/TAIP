"""State pattern for execution lifecycle management."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Protocol

from taip.common.contracts import ExecutionStatus


class ExecutionStateContext(Protocol):
    """Context operations available to execution states."""

    def transition_to(self, state: ExecutionState) -> None:
        ...


class ExecutionState(ABC):
    """Abstract execution state."""

    @property
    @abstractmethod
    def status(self) -> ExecutionStatus:
        ...

    @abstractmethod
    def start(self, context: ExecutionStateContext) -> None:
        ...

    @abstractmethod
    def complete(self, context: ExecutionStateContext) -> None:
        ...

    @abstractmethod
    def fail(self, context: ExecutionStateContext) -> None:
        ...


class PendingState(ExecutionState):
    """Execution is created but has not started."""

    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.PENDING

    @property
    def status(self) -> ExecutionStatus:
        return self.state_status

    def start(self, context: ExecutionStateContext) -> None:
        context.transition_to(RunningState())

    def complete(self, context: ExecutionStateContext) -> None:
        raise ValueError("Cannot complete a pending execution.")

    def fail(self, context: ExecutionStateContext) -> None:
        context.transition_to(FailedState())


class RunningState(ExecutionState):
    """Execution is currently processing model requests."""

    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.RUNNING

    @property
    def status(self) -> ExecutionStatus:
        return self.state_status

    def start(self, context: ExecutionStateContext) -> None:
        raise ValueError("Execution is already running.")

    def complete(self, context: ExecutionStateContext) -> None:
        context.transition_to(CompletedState())

    def fail(self, context: ExecutionStateContext) -> None:
        context.transition_to(FailedState())


class CompletedState(ExecutionState):
    """Execution finished successfully."""

    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.COMPLETED

    @property
    def status(self) -> ExecutionStatus:
        return self.state_status

    def start(self, context: ExecutionStateContext) -> None:
        raise ValueError("Cannot restart a completed execution.")

    def complete(self, context: ExecutionStateContext) -> None:
        raise ValueError("Execution is already completed.")

    def fail(self, context: ExecutionStateContext) -> None:
        raise ValueError("Cannot fail a completed execution.")


class FailedState(ExecutionState):
    """Execution ended in failure."""

    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.FAILED

    @property
    def status(self) -> ExecutionStatus:
        return self.state_status

    def start(self, context: ExecutionStateContext) -> None:
        raise ValueError("Cannot restart a failed execution.")

    def complete(self, context: ExecutionStateContext) -> None:
        raise ValueError("Cannot complete a failed execution.")

    def fail(self, context: ExecutionStateContext) -> None:
        raise ValueError("Execution is already failed.")


class PartialState(ExecutionState):
    """Execution has partially available results."""

    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.PARTIAL

    @property
    def status(self) -> ExecutionStatus:
        return self.state_status

    def start(self, context: ExecutionStateContext) -> None:
        raise ValueError("Cannot restart a partial execution.")

    def complete(self, context: ExecutionStateContext) -> None:
        context.transition_to(CompletedState())

    def fail(self, context: ExecutionStateContext) -> None:
        context.transition_to(FailedState())