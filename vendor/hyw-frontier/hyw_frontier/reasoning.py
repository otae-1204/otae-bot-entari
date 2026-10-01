"""Three logical reasoning tiers mapped to verified model effort values."""
from copy import deepcopy
import json
from pathlib import Path
from .prompt_files import read_prompt

_CONFIG = json.loads(Path(__file__).with_name("reasoning.json").read_text(encoding="utf-8"))
RECONSIDER_PROMPT = read_prompt('reconsider.md')


def needs_reconsideration(previous: dict | None, provider: str, model: str, settings: dict) -> bool:
    """Compare with the last actual model call, including across conversation turns."""
    if previous is None:
        return False
    if (previous.get("provider", provider), previous.get("model", model)) != (provider, model):
        return True
    prior = previous.get("reasoning_settings")
    if not isinstance(prior, dict):
        return False  # Older history has no reliable reasoning configuration to compare.
    if any(prior.get(key) != settings[key] for key in ("mode", "mapping")):
        return True  # A caller explicitly changed mode or one of the three mappings.
    tiers = {"low": 0, "medium": 1, "high": 2}
    efforts = {"off": 0, "low": 1, "medium": 2, "high": 3, "max": 4}
    return (tiers.get(settings["level"], -1) > tiers.get(prior.get("level"), -1)
            or efforts.get(settings["effort"], -1) > efforts.get(prior.get("effort"), -1))


def reasoning_config() -> dict:
    return deepcopy(_CONFIG)


def resolve_reasoning_mode(value: str = "auto") -> str:
    if not isinstance(value, str) or value not in ("auto", *_CONFIG["tiers"]):
        raise ValueError("reasoning_mode 必须为 auto、high、medium 或 low")
    return value


def resolve_reasoning(provider: str, model: str, value: dict[str, str] | None = None) -> dict[str, str] | None:
    profile = next((item for item in _CONFIG["profiles"]
                    if provider in item["providers"] and model.removeprefix("models/") in item["models"]), None)
    if value is None:
        return deepcopy(profile["mapping"]) if profile else None
    if not isinstance(value, dict) or set(value) != set(_CONFIG["tiers"]):
        raise ValueError("reasoning 必须且只能包含 high、medium、low 三个档位")
    if profile is None:
        raise ValueError("当前模型不支持思考等级配置")
    if any(not isinstance(effort, str) or effort not in profile["levels"] for effort in value.values()):
        raise ValueError("当前模型的 reasoning 档位必须为 " + "、".join(profile["levels"]) + "；允许重复")
    return dict(value)
