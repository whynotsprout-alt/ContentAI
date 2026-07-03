from __future__ import annotations

import asyncio
import logging

from agent.runtime.worker import run_worker
from db.session import close_db, init_db


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    try:
        asyncio.run(run_worker.serve())
    finally:
        close_db()


if __name__ == "__main__":
    main()
