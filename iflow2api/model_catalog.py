"""Model catalog used for /v1/models and OpenCode configuration.

Goal: keep this lightweight and editable, without hard-blocking unknown model IDs.
The proxy forwards whatever `model` you send to the upstream iFlow API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    description: str = ""
    # Best-effort metadata for UIs; not enforced.
    context: int = 131072
    output: int = 8192


def get_known_models() -> list[ModelSpec]:
    # A curated superset of commonly available iFlow models.
    # This list is *not* exhaustive; unknown model IDs may still work.
    return [
        ModelSpec(id="glm-4.7", name="GLM-4.7", description="Zhipu GLM-4.7"),
        ModelSpec(id="glm-4.6", name="GLM-4.6", description="Zhipu GLM-4.6"),
        ModelSpec(id="iflow-rome-30ba3b", name="iFlow-ROME-30BA3B", description="iFlow ROME 30B (fast)"),
        ModelSpec(id="deepseek-r1", name="DeepSeek-R1", description="DeepSeek reasoning model"),
        ModelSpec(id="deepseek-v3", name="DeepSeek-V3-671B", description="DeepSeek V3 671B"),
        ModelSpec(id="deepseek-v3.1", name="DeepSeek-V3.1", description="DeepSeek V3.1"),
        ModelSpec(id="deepseek-v3.2", name="DeepSeek-V3.2-Exp", description="DeepSeek V3.2 experimental"),
        ModelSpec(id="deepseek-v3.2-chat", name="DeepSeek-V3.2", description="DeepSeek V3.2 chat"),
        ModelSpec(id="qwen3-coder-plus", name="Qwen3-Coder-Plus", description="Qwen3 coder model"),
        ModelSpec(id="qwen3-max", name="Qwen3-Max", description="Qwen3 flagship"),
        ModelSpec(id="qwen3-max-preview", name="Qwen3-Max-Preview", description="Qwen3 Max preview"),
        ModelSpec(id="qwen3-vl-plus", name="Qwen3-VL-Plus", description="Qwen3 multimodal vision-language"),
        ModelSpec(id="qwen3-32b", name="Qwen3-32B", description="Qwen3 32B"),
        ModelSpec(id="qwen3-235b", name="Qwen3-235B", description="Qwen3 235B"),
        ModelSpec(id="qwen3-235b-a22b-instruct", name="Qwen3-235B-A22B-Instruct", description="Qwen3 235B A22B instruct"),
        ModelSpec(id="qwen3-235b-a22b-thinking-2507", name="Qwen3-235B-A22B-Thinking", description="Qwen3 235B A22B thinking"),
        ModelSpec(id="kimi-k2", name="Kimi-K2", description="Moonshot Kimi K2"),
        ModelSpec(id="kimi-k2-thinking", name="Kimi-K2-Thinking", description="Moonshot Kimi K2 thinking"),
        ModelSpec(id="kimi-k2-0905", name="Kimi-K2-0905", description="Moonshot Kimi K2 0905"),
        ModelSpec(id="minimax-m2", name="MiniMax-M2", description="MiniMax M2"),
        ModelSpec(id="minimax-m2.1", name="MiniMax-M2.1", description="MiniMax M2.1"),
        ModelSpec(id="tstars2.0", name="TStars-2.0", description="iFlow TStars-2.0 assistant"),
    ]


def get_recommended_models() -> list[ModelSpec]:
    """
    A minimal "latest only" set intended for quick client configs (e.g. OpenCode).

    This keeps the UX clean while still covering the common use-cases:
    - general: GLM-4.7
    - small/fast: MiniMax-M2.1
    - coding: Qwen3-Coder-Plus
    - alt/general: DeepSeek-V3.2
    - Moonshot: Kimi-K2-0905
    - iFlow: ROME 30B
    """
    return [
        ModelSpec(id="glm-4.7", name="GLM-4.7", description="Zhipu GLM-4.7"),
        ModelSpec(id="minimax-m2.1", name="MiniMax-M2.1", description="MiniMax M2.1"),
        ModelSpec(id="iflow-rome-30ba3b", name="iFlow-ROME-30BA3B", description="iFlow ROME 30B (fast)"),
        ModelSpec(id="deepseek-v3.2", name="DeepSeek-V3.2", description="DeepSeek V3.2"),
        ModelSpec(id="qwen3-coder-plus", name="Qwen3-Coder-Plus", description="Qwen3 coder model"),
        ModelSpec(id="kimi-k2-0905", name="Kimi-K2-0905", description="Moonshot Kimi K2 0905"),
    ]


def to_openai_models_list(models: Iterable[ModelSpec], *, owned_by: str = "iflow", created: int) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": m.id,
                "object": "model",
                "created": created,
                "owned_by": owned_by,
                "permission": [],
                "root": m.id,
                "parent": None,
            }
            for m in models
        ],
    }


def to_opencode_models(models: Iterable[ModelSpec]) -> dict[str, Any]:
    # OpenCode expects `models` entries with `name`, `limit`, `modalities`, etc.
    out: dict[str, Any] = {}
    for m in models:
        out[m.id] = {
            "name": m.name,
            "limit": {"context": int(m.context), "output": int(m.output)},
            "modalities": {"input": ["text"], "output": ["text"]},
            "capabilities": {"toolCalls": True},
        }
    return out
