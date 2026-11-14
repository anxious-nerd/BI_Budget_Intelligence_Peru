"""Descarga los parquet del MEF desde la carpeta de Drive del equipo a data/raw/.

    python -m scripts.download_data                 # gasto + ingreso (~1.4 GB)
    python -m scripts.download_data --only ingreso  # solo ingreso (~35 MB), útil para probar

Las carpetas deben estar compartidas como "cualquiera con el enlace". Los archivos que ya existen
con el mismo tamaño no se vuelven a bajar.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import gdown

ROOT = Path(__file__).resolve().parent.parent

# Subcarpetas GASTOS e INGRESOS de la carpeta del README:
# https://drive.google.com/drive/folders/16SBvEmBFvqbuuqwO0WiikVK3rTC7Y7Pv
FOLDERS = {
    "gasto": "1b3g2jPeaaL75kRi50IecQn394uEXb3ju",
    "ingreso": "1Zb4WtglpB-WDFpPxEyIpTn3P7FHLDf-2",
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=sorted(FOLDERS), help="bajar solo un dataset")
    p.add_argument("--out", type=Path, default=ROOT / "data" / "raw")
    a = p.parse_args(argv)

    for name, folder_id in FOLDERS.items():
        if a.only and name != a.only:
            continue
        dest = a.out / name
        dest.mkdir(parents=True, exist_ok=True)
        print(f"== {name} -> {dest}")
        files = gdown.download_folder(id=folder_id, output=str(dest), quiet=False, resume=True)
        if not files:
            print(f"No se pudo bajar {name}: ¿la carpeta sigue compartida con enlace?", file=sys.stderr)
            return 1
        print(f"   {len(files)} archivos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
