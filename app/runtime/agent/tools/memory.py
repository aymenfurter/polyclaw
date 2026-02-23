"""Memory search tool -- Foundry IQ vector search over indexed memories."""

from __future__ import annotations

from copilot import define_tool
from pydantic import BaseModel, Field


class SearchMemoriesParams(BaseModel):
    query: str = Field(
        description="Natural language search query to find relevant memories.",
    )
    top: int = Field(default=5, description="Maximum number of results to return (1-10).")


@define_tool(
    description=(
        "Search through indexed memories using Azure AI Search with vector "
        "embeddings. Only works when Foundry IQ is enabled."
    )
)
def search_memories_tool(params: SearchMemoriesParams) -> dict:
    from ...services.foundry_iq import search_memories
    from ...state.foundry_iq_config import get_foundry_iq_config

    config = get_foundry_iq_config()
    if not config.enabled or not config.is_configured:
        return {"status": "skipped", "message": "Foundry IQ is not enabled."}

    try:
        top = min(max(params.top, 1), 10)
        data = search_memories(params.query, top, config)
        if data.get("status") == "ok" and data.get("results"):
            formatted = [
                {
                    "title": r.get("title", ""),
                    "content": r.get("content", ""),
                    "source_type": r.get("source_type", ""),
                    "date": r.get("date", ""),
                }
                for r in data["results"]
            ]
            return {"status": "ok", "results": formatted, "count": len(formatted)}
        return {
            "status": "ok",
            "results": [],
            "count": 0,
            "message": "No matching memories found.",
        }
    except Exception as exc:
        return {"status": "error", "message": f"Memory search failed: {exc}"}
