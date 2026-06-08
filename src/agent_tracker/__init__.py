"""Generic local coordination queue for agent-managed projects."""

from agent_tracker.config import ProjectConfig, load_config
from agent_tracker.service import Coordinator

__all__ = ["Coordinator", "ProjectConfig", "load_config"]
