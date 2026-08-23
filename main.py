import logging

from farmbot.bot import FarmBot
from farmbot.config import load_config
from farmbot.db import Database


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    config = load_config()
    bot = FarmBot(config, Database(config.db_path))
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
