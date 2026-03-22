"""
Web browsing tool — fetch and extract text content from web pages.

Uses trafilatura for high-quality text extraction, with fallback to
BeautifulSoup and finally raw httpx.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("swarm.tools.web_browser")

# Max content length to return (prevents token overflow)
MAX_CONTENT_LENGTH = 8000


@dataclass
class PageContent:
    url: str
    title: str
    text: str
    success: bool
    error: str = ""


class WebBrowserTool:
    async def fetch_page(self, url: str) -> PageContent:
        """Fetch a web page and extract its main text content."""
        # Try 1: trafilatura (best quality extraction)
        result = await self._trafilatura_fetch(url)
        if result.success:
            return result

        # Try 2: BeautifulSoup
        result = await self._bs4_fetch(url)
        if result.success:
            return result

        # Try 3: raw httpx with basic HTML stripping
        return await self._raw_fetch(url)

    async def _trafilatura_fetch(self, url: str) -> PageContent:
        """Use trafilatura for high-quality text extraction."""
        try:
            import asyncio
            import trafilatura

            loop = asyncio.get_event_loop()

            # trafilatura is sync — run in executor
            def _extract():
                downloaded = trafilatura.fetch_url(url)
                if not downloaded:
                    return None, None
                text = trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=True,
                    favor_recall=True,
                )
                # Extract title
                metadata = trafilatura.extract_metadata(downloaded)
                title = metadata.title if metadata else ""
                return text, title

            text, title = await loop.run_in_executor(None, _extract)

            if text:
                return PageContent(
                    url=url,
                    title=title or "",
                    text=text[:MAX_CONTENT_LENGTH],
                    success=True,
                )
            return PageContent(url=url, title="", text="", success=False, error="trafilatura returned empty")

        except ImportError:
            return PageContent(url=url, title="", text="", success=False, error="trafilatura not installed")
        except Exception as e:
            logger.warning(f"trafilatura failed for {url}: {e}")
            return PageContent(url=url, title="", text="", success=False, error=str(e))

    async def _bs4_fetch(self, url: str) -> PageContent:
        """Use BeautifulSoup for text extraction."""
        try:
            import httpx
            from bs4 import BeautifulSoup

            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; SwarmAgent/1.0)"
                })
                resp.raise_for_status()
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            # Remove script, style, nav, footer elements
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
                tag.decompose()

            title = soup.title.string if soup.title else ""

            # Try to find main content area
            main = soup.find("main") or soup.find("article") or soup.find("div", {"role": "main"})
            if main:
                text = main.get_text(separator="\n", strip=True)
            else:
                text = soup.get_text(separator="\n", strip=True)

            # Clean up excessive whitespace
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            text = "\n".join(lines)

            if text:
                return PageContent(
                    url=url,
                    title=title or "",
                    text=text[:MAX_CONTENT_LENGTH],
                    success=True,
                )
            return PageContent(url=url, title="", text="", success=False, error="BeautifulSoup returned empty")

        except ImportError:
            return PageContent(url=url, title="", text="", success=False, error="beautifulsoup4 not installed")
        except Exception as e:
            logger.warning(f"BeautifulSoup failed for {url}: {e}")
            return PageContent(url=url, title="", text="", success=False, error=str(e))

    async def _raw_fetch(self, url: str) -> PageContent:
        """Last resort: raw httpx fetch with basic HTML tag stripping."""
        try:
            import re
            import httpx

            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; SwarmAgent/1.0)"
                })
                resp.raise_for_status()
                html = resp.text

            # Basic HTML stripping
            text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()

            # Try to extract title
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            title = title_match.group(1).strip() if title_match else ""

            return PageContent(
                url=url,
                title=title,
                text=text[:MAX_CONTENT_LENGTH],
                success=bool(text),
                error="" if text else "Page returned empty content",
            )

        except Exception as e:
            logger.error(f"Raw fetch failed for {url}: {e}")
            return PageContent(
                url=url,
                title="",
                text="",
                success=False,
                error=f"Failed to fetch page: {str(e)[:200]}",
            )
