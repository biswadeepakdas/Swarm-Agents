"""
Web search tool — agents can search for documentation, packages, and APIs.

Priority chain:
1. Tavily (designed for AI agents, returns clean content — 1K free/month)
2. Serper.dev (Google results via API — 2.5K free queries)
3. googlesearch-python (scrapes Google HTML — often 429 from cloud IPs)
4. DuckDuckGo (AsyncDDGS)
5. httpx fallback (raw DuckDuckGo HTML scrape)
"""

from __future__ import annotations

import logging
import os
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
        # Try 1: Tavily (best for agents — returns pre-extracted content)
        results = await self._tavily_search(query, max_results)
        if results:
            return results

        # Try 2: Serper.dev (Google results via API)
        results = await self._serper_search(query, max_results)
        if results:
            return results

        # Try 3: googlesearch-python (often 429 from cloud IPs)
        results = await self._google_search(query, max_results)
        if results:
            return results

        # Try 4: DuckDuckGo
        results = await self._ddg_search(query, max_results)
        if results:
            return results

        # Try 5: raw httpx fallback
        return await self._httpx_fallback(query, max_results)

    async def _tavily_search(self, query: str, max_results: int) -> list[SearchResult]:
        """Use Tavily Search API — purpose-built for AI agents."""
        api_key = os.getenv("TAVILY_API_KEY", "")
        if not api_key:
            return []
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": api_key,
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                        "include_answer": True,
                        "include_raw_content": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = []
            # Tavily provides an AI-generated answer — include it as first result
            answer = data.get("answer")
            if answer:
                results.append(SearchResult(
                    title="Tavily AI Answer",
                    url="",
                    snippet=answer[:500],
                ))

            for item in data.get("results", [])[:max_results]:
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", "")[:400],
                ))
            logger.info(f"Tavily search returned {len(results)} results for: {query[:50]}")
            return results

        except ImportError:
            logger.debug("httpx not installed for Tavily")
            return []
        except Exception as e:
            logger.warning(f"Tavily search failed: {e}")
            return []

    async def _serper_search(self, query: str, max_results: int) -> list[SearchResult]:
        """Use Serper.dev — Google Search results via API (2.5K free queries)."""
        api_key = os.getenv("SERPER_API_KEY", "")
        if not api_key:
            return []
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    json={"q": query, "num": max_results},
                    headers={
                        "X-API-KEY": api_key,
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = []

            # Include knowledge graph if available
            kg = data.get("knowledgeGraph")
            if kg and kg.get("description"):
                results.append(SearchResult(
                    title=kg.get("title", "Knowledge Graph"),
                    url=kg.get("website", ""),
                    snippet=kg.get("description", "")[:400],
                ))

            # Include answer box if available
            answer = data.get("answerBox")
            if answer:
                snippet = answer.get("answer") or answer.get("snippet") or answer.get("title", "")
                if snippet:
                    results.append(SearchResult(
                        title="Google Answer",
                        url=answer.get("link", ""),
                        snippet=str(snippet)[:400],
                    ))

            # Organic results
            for item in data.get("organic", [])[:max_results]:
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", "")[:400],
                ))

            logger.info(f"Serper search returned {len(results)} results for: {query[:50]}")
            return results

        except ImportError:
            logger.debug("httpx not installed for Serper")
            return []
        except Exception as e:
            logger.warning(f"Serper search failed: {e}")
            return []

    async def _google_search(self, query: str, max_results: int) -> list[SearchResult]:
        """Use googlesearch-python package."""
        try:
            import asyncio
            from googlesearch import search as gsearch

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
