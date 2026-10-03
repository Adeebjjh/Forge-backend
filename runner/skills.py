"""AI-built skills: stored definitions the agent can load into a run."""
import json
import os
import re
from pathlib import Path

MAX_INSTRUCTIONS = 8000

SKILL_BUILDER_SYSTEM = (
    'You are a skill builder inside the Forge Agent app. The user describes a reusable capability '
    'they want their coding agent to have. Design one focused skill and reply with ONLY a JSON object '
    '(no markdown fences, no commentary) with these keys:\n'
    '- "name": short skill name, 3-40 characters, letters/numbers/spaces/dashes only\n'
    '- "description": one sentence saying what the skill does and when to use it\n'
    '- "instructions": the system-prompt addition for the agent, concrete and actionable, max 4000 characters. '
    'Reference the available tools by name where relevant.\n'
    '- "tools": array of tool names the skill needs, chosen from the available tools list, or an empty array '
    'if it only needs guidance.\n'
    'Keep the skill narrow: one job, clear trigger, explicit steps. Never include credentials, and never '
    'ask the agent to reveal secrets.'
)


def skills_dir():
    override = os.environ.get('FORGE_SKILLS_DIR', '').strip()
    root = Path(override).expanduser() if override else Path.home() / '.forge' / 'skills'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path():
    return skills_dir() / 'skills.json'


def list_skills():
    builtin = _builtin_skills()
    names = {s['name'] for s in builtin}
    try:
        data = json.loads(_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = []
    user = [s for s in data if isinstance(s, dict) and s.get('name') and s['name'] not in names]
    return builtin + user


def _builtin_skills():
    try:
        data = json.loads((Path(__file__).resolve().parent / 'builtin_skills.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict) and s.get('name')]


def get_skill(name):
    for s in list_skills():
        if s.get('name') == name:
            return s
    return None


def validate_skill(skill, known_tools=()):
    if not isinstance(skill, dict):
        raise ValueError('Skill must be an object.')
    name = skill.get('name', '')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 \-]{2,39}', name.strip()):
        raise ValueError('Skill name must be 3-40 characters: letters, numbers, spaces, or dashes.')
    description = skill.get('description', '')
    if not isinstance(description, str) or not description.strip() or len(description) > 500:
        raise ValueError('Skill needs a one-sentence description (max 500 characters).')
    instructions = skill.get('instructions', '')
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > MAX_INSTRUCTIONS:
        raise ValueError(f'Skill instructions must be 1-{MAX_INSTRUCTIONS} characters.')
    tools = skill.get('tools', [])
    if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
        raise ValueError('Skill tools must be an array of tool names.')
    unknown = [t for t in tools if t not in known_tools] if known_tools else []
    if unknown:
        raise ValueError('Unknown tools for this skill: ' + ', '.join(unknown))
    return {'name': name.strip(), 'description': description.strip(),
            'instructions': instructions.strip(), 'tools': list(tools)}


def save_skill(skill, known_tools=()):
    clean = validate_skill(skill, known_tools)
    skills = [s for s in list_skills() if s.get('name') != clean['name']]
    skills.append(clean)
    _path().write_text(json.dumps(skills, indent=2), encoding='utf-8')
    return clean


def delete_skill(name):
    skills = [s for s in list_skills() if s.get('name') != name]
    _path().write_text(json.dumps(skills, indent=2), encoding='utf-8')
    return True


def build_skill(profile, goal, tool_names):
    """Ask the configured provider to design a skill; returns the parsed definition."""
    from .providers import Provider
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
        raise ValueError('Describe the skill you want in 1-4000 characters.')
    provider = Provider(profile, [{'role': 'user', 'content':
        'Available tools: ' + ', '.join(tool_names) + '\n\nSkill request: ' + goal.strip()}],
        system_extra=SKILL_BUILDER_SYSTEM)
    text, _ = provider.turn([], timeout=120)
    raw = text.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
    try:
        definition = json.loads(raw)
    except ValueError:
        raise ValueError('The model did not return a valid skill definition. Try rephrasing the request.') from None
    return validate_skill(definition, tool_names)
