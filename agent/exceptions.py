class AgentError(Exception):
    """Base class for all agent errors."""


class GoalFailedError(AgentError):
    """The agent determined the goal cannot be completed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MaxStepsExceededError(AgentError):
    """The agent ran out of steps without completing the goal."""


class ActionParseError(AgentError):
    """Claude returned a response that could not be parsed as a valid action."""


class BrowserError(AgentError):
    """An error occurred in the browser controller."""
