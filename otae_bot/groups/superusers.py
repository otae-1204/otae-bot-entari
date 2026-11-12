"""Account-independent checks against the configured bot operators."""

from otae_bot.config.settings import Config


def is_superuser(event) -> bool:
    user_id = str(getattr(getattr(event, "user", None), "id", "") or "")
    configured = Config.SUPERUSERS
    values = configured if isinstance(configured, (list, tuple, set)) else (configured,)
    return bool(user_id) and user_id in {str(value) for value in values}
