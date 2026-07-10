"""GoalFlow v2 cloud agent — a GENERAL goal-based agent (not meal-specific).

Owns conversation + memory, interprets fuzzy goals into generic Task
Contracts (CONTRACT v2, see /CONTRACT.md), holds the HITL approval pause
(LangGraph interrupt + checkpointer), and acts as the WebSocket hub relaying
the device's agent_event stream / plan / proposals to the UI.
"""

__version__ = "2.0.0"
