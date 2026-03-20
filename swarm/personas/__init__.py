"""
Persona prompt registry.
Maps role names to system prompt builders.
"""

from swarm.personas.architect import ARCHITECT_PROMPT
from swarm.personas.backend_engineer import BACKEND_ENGINEER_PROMPT
from swarm.personas.frontend_engineer import FRONTEND_ENGINEER_PROMPT
from swarm.personas.designer import DESIGNER_PROMPT
from swarm.personas.reviewer import REVIEWER_PROMPT
from swarm.personas.tester import TESTER_PROMPT
from swarm.personas.devops import DEVOPS_PROMPT
from swarm.personas.product_manager import PRODUCT_MANAGER_PROMPT
from swarm.personas.researcher import RESEARCHER_PROMPT

PERSONA_PROMPTS: dict[str, str] = {
    "architect": ARCHITECT_PROMPT,
    "backend_engineer": BACKEND_ENGINEER_PROMPT,
    "frontend_engineer": FRONTEND_ENGINEER_PROMPT,
    "designer": DESIGNER_PROMPT,
    "reviewer": REVIEWER_PROMPT,
    "tester": TESTER_PROMPT,
    "devops": DEVOPS_PROMPT,
    "product_manager": PRODUCT_MANAGER_PROMPT,
    "researcher": RESEARCHER_PROMPT,
}


def get_persona_prompt(role: str) -> str:
    return PERSONA_PROMPTS.get(role, BACKEND_ENGINEER_PROMPT)
