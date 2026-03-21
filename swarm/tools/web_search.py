"""
Web search tool — agents can search for documentation, packages, and APIs.
Supports multiple backends: googlesearch-python (default), DuckDuckGo, httpx fallback.
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
        """Search the web — tries multiple backends in order."""
        # Try 1: googlesearch-python (most reliable in containers)
        results = await self._google_search(query, max_results)
        if results:
            return results

        # Try 2: DuckDuckGo
        results = await self._ddg_search(query, max_results)
        if results:
            return results

        # Try 3: raw httpx fallback
        return await self._httpx_fallback(query, max_results)

    async def _google_search(self, query: str, max_results: int) -> list[SearchResult]:
        """Use googlesearch-python package."""
        try:
            import asyncio
            from googlesearch import search as gsearch

            # googlesearch is sync, run in executor
            loop = asyncio.get_event_loop()
            urls = await loop.run_in_executor(
                None,
                lambda: list(gsearch(query, num_results=max_results, lang="en"))
            )
            return [
                SearchResult(title=url.split("/")[-1] or url, url=url, snippet=f"Result for: {query}")
                for url in urls
            ]
        except ImportError:
            logger.debug("googlesearch-python not installed")
            return []
        except Exception as e:
            logger.warning(f"Google search failed: {e}")
            return []

    async def _ddg_search(self, query: str, max_results: int) -> list[SearchResult]:
        """Use DuckDuckGo search."""
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
            logger.debug("duckduckgo-search not installed")
            return []
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
            return []

    async def _httpx_fallback(self, query: str, max_results: int) -> list[SearchResult]:
        """Fallback: use httpx to hit DuckDuckGo HTML directly."""
        try:
            import httpx
            import re

            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
                )
                text = resp.text
                results = []
                links = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.+?)</a>', text)
                snippets = re.findall(r'<a class="result__snippet"[^>]*>(.+?)</a>', text)
                for i, (url, title) in enumerate(links[:max_results]):
                    snippet = snippets[i] if i < len(snippets) else ""
                    title = re.sub(r'<[^>]+>', '', title)
                    snippet = re.sub(r'<[^>]+>', '', snippet)
                    results.append(SearchResult(title=title.strip(), url=url.strip(), snippet=snippet.strip()))
                return results
        except Exception as e:
            logger.error(f"All search backends failed: {e}")
            return [SearchResult(
                title="Search unavailable",
                url="",
                snippet=f"Web search is currently unavailable. Proceed with existing knowledge. Query: {query}",
            )]
