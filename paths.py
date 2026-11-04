"""Where the dashboard looks for agent data. Every location can be overridden by an environment variable.

  CLAUDE_CONFIG_DIR          Claude Code config dir (default ~/.claude); projects/, jobs/, sessions/ live under it
  CODEX_HOME                 Codex home (default ~/.codex); sessions/ lives under it
  AGENT_DASH_PI_SESSIONS     Pi session transcripts (default ~/.pi/agent/sessions)
  AGENT_DASH_ANTIGRAVITY_DIR Google Antigravity CLI data: conversations/*.db and conversation_summaries.db
                             (default ~/.gemini/antigravity-cli)
  AGENT_DASH_DSH_DIR         DeepSeek Harness data: storages/session_projcache/sessions/*.json (default ~/.dsh)
  AGENT_DASH_ROSTER_DIR      roster files that name agents (default <CLAUDE_CONFIG_DIR>/agent-roster)
  AGENT_DASH_SHARED_DIR      optional folder several agents use to pass work to each other; when set, every
                             touch inside it is drawn as one shared "area" node (default: unset, feature off)
  AGENT_DASH_SIM_ROOT        workspaces for newsroom.py (default ~/newsroom-sim)
  AGENT_DASH_STATE_DIR       where the server records that it is running and how it last stopped
                             (default ~/.agent-dashboard)
"""
import os
from pathlib import Path


def _dir(env, default):
    value = os.environ.get(env)
    return Path(os.path.expanduser(value)) if value else default


HOME = Path.home()
CLAUDE_DIR = _dir("CLAUDE_CONFIG_DIR", HOME / ".claude")
PROJECTS_DIR = CLAUDE_DIR / "projects"
ROSTER_DIR = _dir("AGENT_DASH_ROSTER_DIR", CLAUDE_DIR / "agent-roster")
CODEX_SESSIONS_DIR = _dir("CODEX_HOME", HOME / ".codex") / "sessions"
PI_SESSIONS_DIR = _dir("AGENT_DASH_PI_SESSIONS", HOME / ".pi" / "agent" / "sessions")
ANTIGRAVITY_DIR = _dir("AGENT_DASH_ANTIGRAVITY_DIR", HOME / ".gemini" / "antigravity-cli")
DSH_DIR = _dir("AGENT_DASH_DSH_DIR", HOME / ".dsh")
SHARED_DIR = _dir("AGENT_DASH_SHARED_DIR", None)
SIM_ROOT = _dir("AGENT_DASH_SIM_ROOT", HOME / "newsroom-sim")
STATE_DIR = _dir("AGENT_DASH_STATE_DIR", HOME / ".agent-dashboard")
