
from __future__ import annotations

from abc import ABC, abstractmethod

from taip.common.contracts import ExecutionStatus


class ExecutionState(ABC):
    """Abstract state of an execution run."""

    @property
    @abstractmethod
    def status(self) -> ExecutionStatus:
        ...

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def complete(self) -> None:
        ...

    @abstractmethod
    def fail(self) -> None:
        ...


class PendingState(ExecutionState):
    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self) -> None:
        ...

    def complete(self) -> None:
        ...

    def fail(self) -> None:
        ...


class RunningState(ExecutionState):
    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self) -> None:
        ...

    def complete(self) -> None:
        ...

    def fail(self) -> None:
        ...


class CompletedState(ExecutionState):
    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self) -> None:
        ...

    def complete(self) -> None:
        ...

    def fail(self) -> None:
        ...


class FailedState(ExecutionState):
    @property
    def status(self) -> ExecutionStatus:
        ...

    def start(self) -> None:
        ...

    def complete(self) -> None:
        ...

    def fail(self) -> None:
        ...
