"""
Media Generation Tool — generates images via API.

Supports multiple providers:
1. DALL-E (OpenAI) — if OPENAI_API_KEY is set
2. Stability AI — if STABILITY_API_KEY is set
3. Placeholder fallback — generates SVG placeholder descriptions

Agents use this to create visual assets: UI mockups, diagrams, logos, etc.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("swarm.tools.media_gen")


@dataclass
class MediaResult:
    success: bool
    media_type: str  # "image/png", "image/svg+xml", etc.
    content: str  # base64 encoded image or SVG text
    url: str = ""  # URL if hosted
    description: str = ""
    error: str = ""
    provider: str = ""


class MediaGenTool:
    """Generate images and visual assets for agents."""

    async def generate_image(
        self,
        prompt: str,
        style: str = "professional",
        size: str = "1024x1024",
    ) -> MediaResult:
        """Generate an image from a text prompt."""

        # Try DALL-E first
        result = await self._dalle_generate(prompt, style, size)
        if result.success:
            return result

        # Try Stability AI
        result = await self._stability_generate(prompt, style, size)
        if result.success:
            return result

        # Fallback: generate SVG placeholder with description
        return self._svg_placeholder(prompt, style, size)

    async def _dalle_generate(
        self, prompt: str, style: str, size: str
    ) -> MediaResult:
        """Generate via OpenAI DALL-E."""
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            return MediaResult(success=False, media_type="", content="", error="No OpenAI key")

        try:
            import httpx

            # Map size to DALL-E supported sizes
            dalle_size = "1024x1024"
            if "512" in size:
                dalle_size = "1024x1024"  # DALL-E 3 minimum
            if "1792" in size or "wide" in size.lower():
                dalle_size = "1792x1024"

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/images/generations",
                    json={
                        "model": "dall-e-3",
                        "prompt": f"{style} style: {prompt}",
                        "n": 1,
                        "size": dalle_size,
                        "response_format": "b64_json",
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()

            img_data = data["data"][0]
            b64 = img_data.get("b64_json", "")
            revised = img_data.get("revised_prompt", prompt)

            return MediaResult(
                success=True,
                media_type="image/png",
                content=b64,
                description=revised,
                provider="dall-e-3",
            )

        except Exception as e:
            logger.debug(f"DALL-E generation failed: {e}")
            return MediaResult(success=False, media_type="", content="", error=str(e))

    async def _stability_generate(
        self, prompt: str, style: str, size: str
    ) -> MediaResult:
        """Generate via Stability AI."""
        api_key = os.getenv("STABILITY_API_KEY", "")
        if not api_key:
            return MediaResult(success=False, media_type="", content="", error="No Stability key")

        try:
            import httpx

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.stability.ai/v2beta/stable-image/generate/core",
                    data={
                        "prompt": f"{style} style: {prompt}",
                        "output_format": "png",
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            b64 = data.get("image", "")
            return MediaResult(
                success=True,
                media_type="image/png",
                content=b64,
                description=prompt,
                provider="stability-ai",
            )

        except Exception as e:
            logger.debug(f"Stability AI generation failed: {e}")
            return MediaResult(success=False, media_type="", content="", error=str(e))

    def _svg_placeholder(
        self, prompt: str, style: str, size: str
    ) -> MediaResult:
        """Generate an SVG placeholder with the prompt as description."""
        w, h = 800, 600
        if "x" in size:
            parts = size.split("x")
            try:
                w, h = int(parts[0]), int(parts[1])
            except ValueError:
                pass

        # Create a styled SVG placeholder
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#1a1a2e;stop-opacity:1" />
      <stop offset="100%" style="stop-color:#16213e;stop-opacity:1" />
    </linearGradient>
  </defs>
  <rect width="{w}" height="{h}" fill="url(#bg)" rx="12"/>
  <rect x="20" y="20" width="{w-40}" height="{h-40}" fill="none" stroke="#0f3460" stroke-width="2" stroke-dasharray="8,4" rx="8"/>
  <text x="{w//2}" y="{h//2 - 30}" text-anchor="middle" fill="#e94560" font-family="system-ui" font-size="24" font-weight="600">
    MEDIA ASSET
  </text>
  <text x="{w//2}" y="{h//2 + 10}" text-anchor="middle" fill="#a0a0b0" font-family="system-ui" font-size="14">
    {prompt[:60]}{"..." if len(prompt)>60 else ""}
  </text>
  <text x="{w//2}" y="{h//2 + 40}" text-anchor="middle" fill="#606080" font-family="system-ui" font-size="12">
    Style: {style} | Size: {w}x{h}
  </text>
  <text x="{w//2}" y="{h-30}" text-anchor="middle" fill="#404060" font-family="system-ui" font-size="10">
    Generated by Swarm Agents — replace with real image via DALL-E or Stability AI
  </text>
</svg>'''

        b64 = base64.b64encode(svg.encode()).decode()
        return MediaResult(
            success=True,
            media_type="image/svg+xml",
            content=b64,
            description=f"SVG placeholder for: {prompt}",
            provider="svg-placeholder",
        )
