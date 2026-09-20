"""AI agent CLI integration: detect installed agents and register this tool's MCP server with them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentSpec:
    id: str
    name: str
    command: str  # executable name
    start_args: tuple[str, ...] = ()
    login_args: tuple[str, ...] | None = None  # None -> login happens inside the agent (slash command)
    login_hint: str = ""
    install_hint: str = ""
    config_kind: str = "none"  # claude-cli | json | toml | none
    config_path: str = ""  # for json/toml kinds (~ expanded)
    config_key: str = "mcpServers"
    supports_mcp: bool = True
    notes: str = ""
    extra_start_args: tuple[str, ...] = field(default_factory=tuple)


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="claude",
        name="Claude Code",
        command="claude",
        login_hint="Run the agent and type /login (or use `claude auth login` on newer versions).",
        install_hint="npm install -g @anthropic-ai/claude-code   (https://docs.claude.com/claude-code)",
        config_kind="claude-cli",
        notes="MCP server registered with `claude mcp add --scope user`.",
    ),
    AgentSpec(
        id="codex",
        name="OpenAI Codex CLI",
        command="codex",
        login_args=("login",),
        login_hint="`codex login` opens the browser sign-in.",
        install_hint="npm install -g @openai/codex   (https://github.com/openai/codex)",
        config_kind="toml",
        config_path="~/.codex/config.toml",
        config_key="mcp_servers",
    ),
    AgentSpec(
        id="gemini",
        name="Gemini CLI",
        command="gemini",
        login_hint="Sign-in is prompted on first start (Google account or API key).",
        install_hint="npm install -g @google/gemini-cli   (https://github.com/google-gemini/gemini-cli)",
        config_kind="json",
        config_path="~/.gemini/settings.json",
    ),
    AgentSpec(
        id="copilot",
        name="GitHub Copilot CLI",
        command="copilot",
        login_hint="Run the agent and type /login.",
        install_hint="npm install -g @github/copilot",
        config_kind="json",
        config_path="~/.copilot/mcp-config.json",
    ),
    AgentSpec(
        id="aider",
        name="Aider",
        command="aider",
        login_hint="Set OPENAI_API_KEY / ANTHROPIC_API_KEY in the environment before starting.",
        install_hint="pip install aider-chat",
        config_kind="none",
        supports_mcp=False,
        notes="Aider has no MCP client; use the CLI/export files with it instead.",
    ),
    AgentSpec(
        id="shell",
        name="Shell",
        command=os.environ.get("SHELL", "cmd" if sys.platform == "win32" else "bash"),
        login_hint="",
        install_hint="",
        config_kind="none",
        supports_mcp=False,
        notes="Plain shell - run `edb-explorer` commands or any other agent by hand.",
    ),
)


def agent_by_id(agent_id: str) -> AgentSpec:
    for a in AGENTS:
        if a.id == agent_id:
            return a
    raise KeyError(agent_id)


def find_executable(command: str) -> str | None:
    if os.path.isabs(command) and os.path.exists(command):
        return command
    found = shutil.which(command)
    if found:
        return found
    if sys.platform == "win32":
        for ext in (".cmd", ".exe", ".bat"):
            found = shutil.which(command + ext)
            if found:
                return found
    return None


def mcp_server_command(allowed: list[str] | None = None) -> list[str]:
    """Command line that starts this tool's MCP server (frozen bundle, installed script or `python -m`)."""
    args: list[str]
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("edb-explorer.exe" if sys.platform == "win32" else "edb-explorer")
        args = [str(exe if exe.exists() else sys.executable), "mcp"]
    else:
        script = find_executable("edb-explorer")
        args = [script, "mcp"] if script else [sys.executable, "-m", "edb_explorer", "mcp"]
    for d in allowed or []:
        args += ["--allow", d]
    return args


def mcp_config_snippet(allowed: list[str] | None = None) -> dict[str, Any]:
    cmd = mcp_server_command(allowed)
    return {"mcpServers": {"edb-explorer": {"command": cmd[0], "args": cmd[1:]}}}


def _write_json_config(path: Path, key: str, cmd: list[str]) -> str:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy(path, backup)
            data = {}
    servers = data.setdefault(key, {})
    servers["edb-explorer"] = {"command": cmd[0], "args": cmd[1:]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f"Registered edb-explorer in {path}"


def _write_toml_config(path: Path, table: str, cmd: list[str]) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    header = f"[{table}.edb-explorer]"
    block = f"{header}\ncommand = {json.dumps(cmd[0])}\nargs = {json.dumps(cmd[1:])}\n"
    if header in text:
        start = text.index(header)
        end = text.find("\n[", start + 1)
        text = text[:start] + block + (text[end + 1 :] if end >= 0 else "")
    else:
        text = text.rstrip() + ("\n\n" if text.strip() else "") + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"Registered edb-explorer in {path}"


def configure_agent(agent: AgentSpec, allowed: list[str] | None = None) -> str:
    """Register the MCP server with an agent. Returns a human-readable result."""
    if not agent.supports_mcp:
        return f"{agent.name} has no MCP client. Use the CLI (`edb-explorer sql/export/report`) from its shell instead."
    cmd = mcp_server_command(allowed)
    if agent.config_kind == "claude-cli":
        exe = find_executable(agent.command)
        if not exe:
            return f"{agent.name} is not installed. {agent.install_hint}"
        subprocess.run(
            [exe, "mcp", "remove", "--scope", "user", "edb-explorer"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        res = subprocess.run(
            [exe, "mcp", "add", "--scope", "user", "edb-explorer", "--", *cmd],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        out = (res.stdout + res.stderr).strip()
        return out or f"Registered edb-explorer with {agent.name} (user scope)."
    path = Path(os.path.expanduser(agent.config_path))
    if agent.config_kind == "json":
        return _write_json_config(path, agent.config_key, cmd)
    if agent.config_kind == "toml":
        return _write_toml_config(path, agent.config_key, cmd)
    return "Nothing to configure."


def agent_status(agent: AgentSpec) -> dict[str, Any]:
    exe = find_executable(agent.command)
    version = None
    if exe and agent.id != "shell":
        try:
            res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, check=False)
            version = (
                (res.stdout or res.stderr).strip().splitlines()[0][:80] if (res.stdout or res.stderr).strip() else None
            )
        except (OSError, subprocess.TimeoutExpired):
            version = None
    return {
        "id": agent.id,
        "name": agent.name,
        "installed": exe is not None,
        "path": exe,
        "version": version,
        "supports_mcp": agent.supports_mcp,
        "install_hint": agent.install_hint,
        "login_hint": agent.login_hint,
    }
