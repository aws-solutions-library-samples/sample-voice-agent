"""Built-in tools for the voice agent.

For pipeline registration, use the catalog:
    >>> from app.tools.builtin.catalog import ALL_LOCAL_TOOLS

For individual tool access:
    >>> from app.tools.builtin import time_tool, transfer_tool, end_call_tool
"""

from .time_tool import time_tool
from .transfer_tool import transfer_tool
from .end_call_tool import end_call_tool
from .press_digit_tool import press_digit_tool
from .catalog import ALL_LOCAL_TOOLS

__all__ = [
    # Catalog (preferred for pipeline registration)
    "ALL_LOCAL_TOOLS",
    # Individual tools
    "time_tool",
    "transfer_tool",
    "end_call_tool",
    "press_digit_tool",
]
