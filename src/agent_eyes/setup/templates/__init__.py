"""Templates for agent-eyes configuration across AI tools."""

from .skill import SKILL_MD
from .openai_skill import OPENAI_YAML
from .claude_md import CLAUDE_MD_SECTION
from .mcp_entry import get_mcp_entry, get_agent_eyes_tools_list

__all__ = [
    "SKILL_MD",
    "OPENAI_YAML",
    "CLAUDE_MD_SECTION",
    "get_mcp_entry",
    "get_agent_eyes_tools_list",
]
