"""Generic condition polling for E2E tests."""

import time
from collections.abc import Callable


def poll(condition: Callable[[], bool], timeout_s: float, interval_s: float = 5, msg: str = "") -> None:
    """Wait until condition() returns true; raise TimeoutError(msg) after timeout_s."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if condition():
            return
        time.sleep(interval_s)
    raise TimeoutError(f"Condition not met within {timeout_s}s: {msg}")
