"""Editable model instructions, kept separately from schemas and program state."""
from pathlib import Path

PROMPT_DIR = Path(__file__).with_name('prompts')


def read_prompt(name: str, **values) -> str:
    text = (PROMPT_DIR / name).read_text(encoding='utf-8').strip()
    if not text:
        raise ValueError(f'Empty prompt: {name}')
    for key, value in values.items():
        text = text.replace('{{' + key + '}}', str(value))
    return text
