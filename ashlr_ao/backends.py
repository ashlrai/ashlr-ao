"""
Ashlr AO — Backend Definitions

Rich configuration for supported agent backends (Claude Code, Codex, Aider, Goose).
"""

from dataclasses import dataclass, field


@dataclass
class BackendConfig:
    """Rich configuration for an agent backend CLI tool."""
    command: str
    args: list[str] = field(default_factory=list)
    available: bool = False
    # Orchestration capabilities
    supports_json_output: bool = False
    supports_system_prompt: bool = False
    supports_tool_restriction: bool = False
    supports_session_resume: bool = False
    supports_model_select: bool = False
    supports_prompt_arg: bool = False  # If True, task is passed as CLI positional arg (not via send-keys)
    # Automation
    auto_approve_flag: str = ""
    plan_mode_flag: str = ""  # CLI flag to enable plan/review mode (e.g. "--permission-mode plan")
    inject_role_prompt: bool = True  # Whether to inject role system prompt (disable for tools with their own context)
    # Status detection overrides (merge with defaults)
    status_patterns: dict[str, list[str]] = field(default_factory=dict)
    # Cost rates (per 1K tokens)
    cost_input_per_1k: float = 0.003
    cost_output_per_1k: float = 0.015
    # Context window sizing
    context_window: int = 200_000  # tokens
    char_to_token_ratio: float = 3.5

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "args": self.args,
            "available": self.available,
            "supports_json_output": self.supports_json_output,
            "supports_system_prompt": self.supports_system_prompt,
            "supports_tool_restriction": self.supports_tool_restriction,
            "supports_session_resume": self.supports_session_resume,
            "supports_model_select": self.supports_model_select,
            "supports_prompt_arg": self.supports_prompt_arg,
            "auto_approve_flag": self.auto_approve_flag,
            "plan_mode_flag": self.plan_mode_flag,
            "inject_role_prompt": self.inject_role_prompt,
            "cost_input_per_1k": self.cost_input_per_1k,
            "cost_output_per_1k": self.cost_output_per_1k,
            "context_window": self.context_window,
            "char_to_token_ratio": self.char_to_token_ratio,
        }


# Per-model pricing (input $/1K tokens, output $/1K tokens)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Claude models
    "claude-opus-4-6": (0.015, 0.075),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-haiku-4-5": (0.0008, 0.004),
    "opus": (0.015, 0.075),
    "sonnet": (0.003, 0.015),
    "haiku": (0.0008, 0.004),
    # OpenAI models
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "o1": (0.015, 0.06),
    "o3": (0.01, 0.04),
    # Default fallback
    "default": (0.003, 0.015),
}


def get_model_pricing(model: str, backend: str = "claude-code") -> tuple[float, float]:
    """Get (input_cost_per_1k, output_cost_per_1k) for a model, with backend fallback."""
    if model and model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # Fuzzy match: check if model starts with a known prefix
    for key, rates in MODEL_PRICING.items():
        if model and model.startswith(key):
            return rates
    # Fall back to backend default
    if backend in KNOWN_BACKENDS:
        b = KNOWN_BACKENDS[backend]
        return (b.cost_input_per_1k, b.cost_output_per_1k)
    return MODEL_PRICING["default"]


KNOWN_BACKENDS: dict[str, BackendConfig] = {
    "claude-code": BackendConfig(
        command="claude",
        args=["--dangerously-skip-permissions"],
        supports_json_output=True,
        supports_system_prompt=True,
        supports_tool_restriction=True,
        supports_session_resume=True,
        supports_model_select=True,
        supports_prompt_arg=True,
        auto_approve_flag="--dangerously-skip-permissions",
        plan_mode_flag="--permission-mode plan",
        status_patterns={
            "working": [r"⎿", r"╭─", r"╰─", r"Tool Use:", r"Bash:", r"\$ ",
                        r"esc to interrupt", r"Running ", r"Updated \d+ file",
                        r"Created ", r"Wrote ", r"tokens remaining",
                        r"Reading file", r"Editing file", r"Writing file",
                        r"Searching for", r"Globbing", r"Grepping",
                        r"npm (run|install|test)", r"pip install",
                        r"pytest", r"jest", r"vitest", r"mocha",
                        r"compiling|building|bundling",
                        r"git (add|commit|push|pull|diff|log|status)"],
            "waiting": [r"Do you want to proceed", r"Allow once", r"Allow always",
                        r"Press Enter to retry", r"Type your response",
                        r"waiting for your", r"What would you like",
                        r"Approve", r"Deny", r"Skip",
                        r"Y/n\]", r"y/N\]",
                        r"Enter a value", r"Select an option",
                        r"Choose (a|an|one|which)"],
            "planning": [r"Thinking\.\.\.", r"Schlepping", r"I'll start by",
                         r"Let me analyze", r"Let me plan",
                         r"I need to", r"First,? I('ll| will)",
                         r"Analyzing", r"Understanding"],
            "complete": [r"❯", r"Task completed", r"All done",
                         r"Successfully completed", r"Finished"],
        },
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
        context_window=200_000,
        char_to_token_ratio=3.5,
    ),
    "codex": BackendConfig(
        command="codex",
        args=[],
        supports_json_output=True,
        auto_approve_flag="--full-auto",
        status_patterns={
            "working": [r"Generating", r"Applying changes", r"Reviewing",
                        r"Searching codebase", r"Editing ", r"Writing ",
                        r"Running command", r"Installing", r"Testing",
                        r"Building", r"Compiling"],
            "waiting": [r"Do you want to", r"Confirm", r"Y/n",
                        r"Select an option", r"Enter a value"],
            "planning": [r"Analyzing", r"Understanding", r"Planning",
                         r"Thinking", r"Researching"],
            "complete": [r"Complete", r"Done", r"Finished", r"All changes applied"],
        },
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.012,
        context_window=128_000,
        char_to_token_ratio=3.2,
    ),
    "aider": BackendConfig(
        command="aider",
        args=[],
        supports_model_select=True,
        auto_approve_flag="-y",
        status_patterns={
            "working": [r"Editing ", r"Applied edit to", r"Creating ",
                        r"Committing", r"Running ", r"Added .+ to the chat",
                        r"Searching", r"tokens used", r"Sending"],
            "waiting": [r"Do you want to", r"y/n", r"Enter",
                        r"would you like", r"Type your response",
                        r"aider>"],
            "planning": [r"Thinking", r"Analyzing", r"Understanding",
                         r"Looking at", r"Let me"],
            "complete": [r"Completed", r"All done", r"Finished"],
        },
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
        context_window=128_000,
        char_to_token_ratio=3.5,
    ),
    "goose": BackendConfig(
        command="goose",
        args=[],
        supports_session_resume=True,
        auto_approve_flag="-y",
        status_patterns={
            "working": [r"Executing", r"Running", r"Modifying",
                        r"Creating", r"Writing", r"Editing",
                        r"Installing", r"Building", r"Testing"],
            "waiting": [r"Do you want", r"Confirm", r"Select",
                        r"goose>", r"Enter"],
            "planning": [r"Planning", r"Analyzing", r"Thinking",
                         r"Reviewing", r"Investigating"],
            "complete": [r"Complete", r"Done", r"Finished",
                         r"Successfully"],
        },
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
        context_window=200_000,
        char_to_token_ratio=3.5,
    ),
}
