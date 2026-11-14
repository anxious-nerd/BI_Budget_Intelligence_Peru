"""CLI del ETL.

Ejemplos:
    python -m etl.run                          # todos los años (primera vez)
    python -m etl.run --years 2025 2026        # incremental: solo esos años
    python -m etl.run --years 2026 --only facts cubes
    python -m etl.run --full                   # borra y reconstruye todo
    python -m etl.run --dataset gasto
"""
from __future__ import annotations

import argparse
import logging
import sys

from config import get_settings
from etl import pipeline


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, nargs="+", help="años a procesar (por defecto: todos los del origen)")
    ap.add_argument("--dataset", choices=["gasto", "ingreso"], nargs="+", default=["gasto", "ingreso"])
    ap.add_argument("--only", choices=["dims", "facts", "cubes", "views"], nargs="+",
                    help="ejecutar solo estos pasos (por defecto todos)")  # fmt: skip
    ap.add_argument("--full", action="store_true", help="borra el warehouse y reconstruye todo")
    ap.add_argument("--exec-mode", choices=["cumulative", "flow"], help="sobreescribe MEF_EXEC_MODE")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")  # fmt: skip
    overrides = {"exec_mode": a.exec_mode} if a.exec_mode else {}
    settings = get_settings(**overrides)
    logging.info("warehouse=%s | exec_mode=%s", settings.warehouse, settings.exec_mode)
    try:
        pipeline.run(
            settings, years=a.years, datasets=a.dataset,
            steps=a.only or ("dims", "facts", "cubes", "views"), full=a.full,
        )  # fmt: skip
    except pipeline.PreflightError as e:
        logging.error("%s", e)
        return 2
    logging.info("ETL terminado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
