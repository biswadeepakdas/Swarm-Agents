"""
Multi-Model Council — runs the same prompt through multiple LLMs and synthesizes.

Used for high-impact decisions: architecture, security, financial trade-offs.
Draft → Compare → Decide pipeline per the Perplexity Computer spec.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import litellm

from swarm.config import config
from swarm.core.model_router import MODELS, ModelConfig

logger = logging.getLogger("swarm.council")


@dataclass
class CouncilVote:
    model: str
    content: str
    latency_ms: int
    token_count: int = 0
    error: str | None = None


@dataclass
class CouncilResult:
    question: str
    votes: list[CouncilVote] = field(default_factory=list)
    synthesis: str = ""
    agreement_score: float = 0.0  # 0-1, how much models agree
    chosen_approach: str = ""
    reasoning: str = ""
    total_latency_ms: int = 0


# Models to use in council — pick diverse providers for different perspectives
COUNCIL_MODELS = [
    "claude-sonnet",
    "gpt-4o",
    "nvidia-devstral",
    "nvidia-deepseek-v3",
    "gemini-pro",
]


class Council:
    """Multi-model council for high-impact decisions."""

    def __init__(self) -> None:
        self._available: list[str] = []
        self._detect_available()

    def _detect_available(self) -> None:
        for name in COUNCIL_MODELS:
            cfg = MODELS.get(name)
            if not cfg:
                continue
            key = os.getenv(cfg.api_key_env, "")
            if not key and "nvidia" in name and config.llm_api_key:
                key = config.llm_api_key
            if key:
                self._available.append(name)
        logger.info(f"Council: {len(self._available)} models available: {self._available}")

    async def deliberate(
        self,
        question: str,
        context: str = "",
        max_models: int = 3,
        max_tokens: int = 2000,
    ) -> CouncilResult:
        """
        Run council deliberation:
        1. Draft: ask 2-4 models the same question
        2. Compare: find agreements and disagreements
        3. Decide: synthesize into a single recommendation
        """
        models_to_use = self._available[:max_models]
        if len(models_to_use) < 2:
            # Not enough models — just return single model answer
            if models_to_use:
                vote = await self._get_vote(models_to_use[0], question, context, max_tokens)
                return CouncilResult(
                    question=question,
                    votes=[vote],
                    synthesis=vote.content,
                    agreement_score=1.0,
                    chosen_approach=vote.content,
                    reasoning="Single model (not enough models for council)",
                )
            return CouncilResult(question=question, synthesis="No models available for council.")

        t0 = time.time()

        # Phase 1: Draft — parallel calls to all models
        draft_tasks = [
            self._get_vote(model_name, question, context, max_tokens)
            for model_name in models_to_use
        ]
        votes = await asyncio.gather(*draft_tasks)
        valid_votes = [v for v in votes if not v.error]

        if not valid_votes:
            return CouncilResult(
                question=question,
                votes=list(votes),
                synthesis="All council models failed.",
            )

        # Phase 2: Compare — use the strongest available model to synthesize
        synthesis_model = models_to_use[0]
        synthesis = await self._synthesize(question, valid_votes, synthesis_model, max_tokens)

        total_ms = int((time.time() - t0) * 1000)

        return CouncilResult(
            question=question,
            votes=list(votes),
            synthesis=synthesis.get("synthesis", ""),
            agreement_score=synthesis.get("agreement_score", 0.5),
            chosen_approach=synthesis.get("chosen_approach", ""),
            reasoning=synthesis.get("reasoning", ""),
            total_latency_ms=total_ms,
        )

    async def _get_vote(
        self, model_name: str, question: str, context: str, max_tokens: int
    ) -> CouncilVote:
        """Get a single model's vote."""
        cfg = MODELS.get(model_name)
        if not cfg:
            return CouncilVote(model=model_name, content="", latency_ms=0, error="Model not found")

        prompt = f"You are an expert advisor in a multi-model council. Be independent and decisive — do not hedge.\n\n"
        if context:
            prompt += f"Context:\n{context}\n\n"
        prompt += f"Question:\n{question}\n\nProvide your clear recommendation with reasoning."

        model = cfg.model
        api_base = cfg.api_base
        api_key = os.getenv(cfg.api_key_env, "") or config.llm_api_key

        if api_base and "nvidia" in api_base:
            if not model.startswith("openai/"):
                model = f"openai/{model}"

        t0 = time.time()
        try:
            call_kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "api_key": api_key,
                "timeout": 60,
            }
            if api_base:
                call_kwargs["api_base"] = api_base

            response = await asyncio.wait_for(
                litellm.acompletion(**call_kwargs),
                timeout=65,
            )

            content = response.choices[0].message.content or ""
            tokens = response.usage.total_tokens if response.usage else 0
            latency = int((time.time() - t0) * 1000)

            return CouncilVote(
                model=model_name,
                content=content,
                latency_ms=latency,
                token_count=tokens,
            )

        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            logger.warning(f"Council vote failed for {model_name}: {e}")
            return CouncilVote(
                model=model_name,
                content="",
                latency_ms=latency,
                error=str(e)[:200],
            )

    async def _synthesize(
        self, question: str, votes: list[CouncilVote], model_name: str, max_tokens: int
    ) -> dict[str, Any]:
        """Synthesize multiple votes into a single decision."""
        cfg = MODELS.get(model_name)
        if not cfg:
            # Fallback: just pick the longest answer
            best = max(votes, key=lambda v: len(v.content))
            return {
                "synthesis": best.content,
                "agreement_score": 0.5,
                "chosen_approach": best.content[:200],
                "reasoning": f"Used {best.model}'s response (synthesis model unavailable)",
            }

        votes_text = "\n\n".join(
            f"### {v.model} ({v.latency_ms}ms)\n{v.content}" for v in votes
        )

        synthesis_prompt = f"""You are the council synthesizer. Multiple AI models were asked the same question.
Analyze their responses, identify agreements and disagreements, then provide a final recommendation.

## Question
{question}

## Model Responses
{votes_text}

## Your Task
Respond in this exact JSON format:
{{
  "agreement_score": 0.0 to 1.0 (how much the models agree),
  "synthesis": "Combined recommendation incorporating the best insights from all models",
  "chosen_approach": "The specific approach or decision (1-2 sentences)",
  "reasoning": "Why this approach was chosen, noting key agreements and disagreements"
}}"""

        model = cfg.model
        api_base = cfg.api_base
        api_key = os.getenv(cfg.api_key_env, "") or config.llm_api_key

        if api_base and "nvidia" in api_base:
            if not model.startswith("openai/"):
                model = f"openai/{model}"

        try:
            call_kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": synthesis_prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
                "api_key": api_key,
                "timeout": 60,
            }
            if api_base:
                call_kwargs["api_base"] = api_base

            response = await asyncio.wait_for(
                litellm.acompletion(**call_kwargs),
                timeout=65,
            )

            text = response.choices[0].message.content or ""

            # Try to parse JSON from response
            try:
                # Find JSON in response
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

            return {
                "synthesis": text,
                "agreement_score": 0.5,
                "chosen_approach": text[:200],
                "reasoning": "Could not parse structured synthesis",
            }

        except Exception as e:
            logger.warning(f"Council synthesis failed: {e}")
            best = max(votes, key=lambda v: len(v.content))
            return {
                "synthesis": best.content,
                "agreement_score": 0.5,
                "chosen_approach": best.content[:200],
                "reasoning": f"Synthesis failed ({e}), using {best.model}'s response",
            }


# Singleton
_council: Council | None = None


def get_council() -> Council:
    global _council
    if _council is None:
        _council = Council()
    return _council
