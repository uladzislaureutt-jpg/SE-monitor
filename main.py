from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from social_monitor import run_monitor


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ежедневный мониторинг критических публикаций и жалоб жителей "
            "Беларуси по социально-экономической проблематике."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не отправлять почту и не обновлять state.json/cache.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    project_root = Path(__file__).resolve().parent
    summary = run_monitor(project_root, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
