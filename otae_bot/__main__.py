"""Support ``python -m otae_bot`` alongside the existing bot.py entrypoint."""

from otae_bot.application import main


if __name__ == "__main__":
    raise SystemExit(main())
