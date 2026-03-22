"""
GitHub Integration Tool — agents can push code, create repos, open PRs.

Uses GitHub REST API with a Personal Access Token (GITHUB_TOKEN env var).
No OAuth required — just set the token.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("swarm.tools.github")


@dataclass
class GitHubResult:
    success: bool
    data: dict[str, Any]
    error: str = ""


class GitHubTool:
    """GitHub API operations for agents."""

    def __init__(self) -> None:
        self.token = os.getenv("GITHUB_TOKEN", "")
        self.api_base = "https://api.github.com"

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def create_repo(
        self, name: str, description: str = "", private: bool = False
    ) -> GitHubResult:
        """Create a new GitHub repository."""
        if not self.available:
            return GitHubResult(success=False, data={}, error="GITHUB_TOKEN not set")

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.api_base}/user/repos",
                    json={"name": name, "description": description, "private": private, "auto_init": True},
                    headers=self._headers(),
                )
                if resp.status_code == 201:
                    data = resp.json()
                    return GitHubResult(success=True, data={
                        "repo_url": data["html_url"],
                        "clone_url": data["clone_url"],
                        "full_name": data["full_name"],
                    })
                return GitHubResult(success=False, data={}, error=f"HTTP {resp.status_code}: {resp.text[:200]}")

        except Exception as e:
            return GitHubResult(success=False, data={}, error=str(e)[:200])

    async def create_or_update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str = "Update via Swarm Agents",
        branch: str = "main",
    ) -> GitHubResult:
        """Create or update a file in a repository."""
        if not self.available:
            return GitHubResult(success=False, data={}, error="GITHUB_TOKEN not set")

        import base64

        try:
            import httpx

            b64_content = base64.b64encode(content.encode()).decode()

            async with httpx.AsyncClient(timeout=30) as client:
                # Check if file exists (to get SHA for update)
                existing = await client.get(
                    f"{self.api_base}/repos/{repo}/contents/{path}",
                    params={"ref": branch},
                    headers=self._headers(),
                )
                sha = None
                if existing.status_code == 200:
                    sha = existing.json().get("sha")

                body: dict[str, Any] = {
                    "message": message,
                    "content": b64_content,
                    "branch": branch,
                }
                if sha:
                    body["sha"] = sha

                resp = await client.put(
                    f"{self.api_base}/repos/{repo}/contents/{path}",
                    json=body,
                    headers=self._headers(),
                )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    return GitHubResult(success=True, data={
                        "path": path,
                        "sha": data.get("content", {}).get("sha", ""),
                        "url": data.get("content", {}).get("html_url", ""),
                    })
                return GitHubResult(success=False, data={}, error=f"HTTP {resp.status_code}: {resp.text[:200]}")

        except Exception as e:
            return GitHubResult(success=False, data={}, error=str(e)[:200])

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str = "",
        head: str = "dev",
        base: str = "main",
    ) -> GitHubResult:
        """Create a pull request."""
        if not self.available:
            return GitHubResult(success=False, data={}, error="GITHUB_TOKEN not set")

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.api_base}/repos/{repo}/pulls",
                    json={"title": title, "body": body, "head": head, "base": base},
                    headers=self._headers(),
                )
                if resp.status_code == 201:
                    data = resp.json()
                    return GitHubResult(success=True, data={
                        "pr_number": data["number"],
                        "pr_url": data["html_url"],
                        "state": data["state"],
                    })
                return GitHubResult(success=False, data={}, error=f"HTTP {resp.status_code}: {resp.text[:200]}")

        except Exception as e:
            return GitHubResult(success=False, data={}, error=str(e)[:200])

    async def get_user(self) -> GitHubResult:
        """Get authenticated user info."""
        if not self.available:
            return GitHubResult(success=False, data={}, error="GITHUB_TOKEN not set")

        try:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.api_base}/user",
                    headers=self._headers(),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return GitHubResult(success=True, data={
                        "login": data["login"],
                        "name": data.get("name", ""),
                        "repos_url": data["repos_url"],
                    })
                return GitHubResult(success=False, data={}, error=f"HTTP {resp.status_code}")

        except Exception as e:
            return GitHubResult(success=False, data={}, error=str(e)[:200])
