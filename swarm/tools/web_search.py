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
            logger.warning("duckduckgo-search not installed — trying fallback")
            return await self._fallback_search(query, max_results)
        except Exception as e:
            logger.error(f"Web search failed: {e}")
            return []

    async def _fallback_search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Fallback: use httpx to hit DuckDuckGo HTML directly."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                # Simple extraction of result snippets from HTML
                text = resp.text
                results = []
                # Find result blocks
                import re
                links = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.+?)</a>', text)
                snippets = re.findall(r'<a class="result__snippet"[^>]*>(.+?)</a>', text)
                for i, (url, title) in enumerate(links[:max_results]):
                    snippet = snippets[i] if i < len(snippets) else ""
                    # Clean HTML tags
                    title = re.sub(r'<[^>]+>', '', title)
                    snippet = re.sub(r'<[^>]+>', '', snippet)
                    results.append(SearchResult(title=title.strip(), url=url.strip(), snippet=snippet.strip()))
                return results
        except Exception as e:
            logger.error(f"Fallback web search also failed: {e}")
            return [SearchResult(
                title="Search unavailable",
                url="",
                snippet=f"Web search is currently unavailable. Please proceed with your existing knowledge. Query was: {query}",
            )]
