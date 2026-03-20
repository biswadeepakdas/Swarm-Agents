"""
Web search tool — agents can search for documentation, packages, and APIs.
Uses DuckDuckGo (no API key needed) as the default provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("swarm.tools.web_search")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class WebSearchTool:
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Search the web using DuckDuckGo."""
        try:
            from duckduckgo_search import AsyncDDGS

            async with AsyncDDGS() as ddgs:
                results = []
                async for r in ddgs.atext(query, max_results=max_results):
                    results.append(SearchResult(
                        title=r.get("title", ""),
                        url=r.get("href", ""),
                        snippet=r.get("body", ""),
                    ))
                return results
        except ImportError:
            logger.warning("duckduckgo-search not installed — web search disabled")
            return []
        except Exception as e:
            logger.error(f"Web search failed: {e}")
            return []
