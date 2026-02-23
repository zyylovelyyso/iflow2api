"""Model catalog - Latest 3 flagship models only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    description: str = ""
    context: int = 200000
    output: int = 8192


def get_known_models() -> list[ModelSpec]:
    """Top 3 latest flagship models."""
    return [
        ModelSpec(id="glm-5", name="GLM-5", description="Zhipu GLM-5 744B MoE Flagship", context=200000, output=128000),
        ModelSpec(id="minimax-m2.5", name="MiniMax-M2.5", description="MiniMax M2.5 Agentic", context=200000),
        ModelSpec(id="kimi-k2.5", name="Kimi-K2.5", description="Moonshot Kimi K2.5 Multimodal", context=262144),
    ]


def get_recommended_models() -> list[ModelSpec]:
    return get_known_models()


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
    out: dict[str, Any] = {}
    for m in models:
        out[m.id] = {
            "name": m.name,
            "limit": {"context": int(m.context), "output": int(m.output)},
            "modalities": {"input": ["text"], "output": ["text"]},
            "capabilities": {"toolCalls": True},
        }
    return out
