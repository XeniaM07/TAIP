
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Protocol

from taip.common.contracts import ExecutionStatus


class ExecutionStateContext(Protocol):
    """Context operations available to execution states."""

    def transition_to(self, state: ExecutionState) -> None:
        ...


class ExecutionState(ABC):
    """Abstract state of an execution run."""

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
    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.PENDING

    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self, context: ExecutionStateContext) -> None:
        ...

    def complete(self, context: ExecutionStateContext) -> None:
        ...

    def fail(self, context: ExecutionStateContext) -> None:
        ...


class RunningState(ExecutionState):
    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.RUNNING

    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self, context: ExecutionStateContext) -> None:
        ...

    def complete(self, context: ExecutionStateContext) -> None:
        ...

    def fail(self, context: ExecutionStateContext) -> None:
        ...


class CompletedState(ExecutionState):
    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.COMPLETED

    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self, context: ExecutionStateContext) -> None:
        ...

    def complete(self, context: ExecutionStateContext) -> None:
        ...

    def fail(self, context: ExecutionStateContext) -> None:
        ...


class FailedState(ExecutionState):
    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.FAILED

    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self, context: ExecutionStateContext) -> None:
        ...

    def complete(self, context: ExecutionStateContext) -> None:
        ...

    def fail(self, context: ExecutionStateContext) -> None:
        ...


class PartialState(ExecutionState):
    state_status: ClassVar[ExecutionStatus] = ExecutionStatus.PARTIAL

    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self, context: ExecutionStateContext) -> None:
        ...

    def complete(self, context: ExecutionStateContext) -> None:
        ...

    def fail(self, context: ExecutionStateContext) -> None:
        ...
