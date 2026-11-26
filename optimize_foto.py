#!/usr/bin/env python3
"""Ottimizza in place le foto dei verbali (sopralluoghi/foto/VBS-xxxx/*.jpg).
Uso:   python optimize_foto.py <cartella_foto> [--max 1600] [--quality 80] [--min-kb 400] [--dry-run]
Requisiti: pip install pillow
Mantiene nomi e estensione (i link salvati nei verbali restano validi), applica l'orientamento EXIF,
sovrascrive solo se il file diventa più piccolo.
"""
import argparse, io, sys
from pathlib import Path
from PIL import Image, ImageOps

ap = argparse.ArgumentParser()
ap.add_argument("folder")
ap.add_argument("--max", type=int, default=1600, help="lato lungo massimo in px")
ap.add_argument("--quality", type=int, default=80)
ap.add_argument("--min-kb", type=int, default=400, help="ignora file più piccoli di così")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

tot_in = tot_out = n = skip = 0
for p in sorted(Path(a.folder).rglob("*")):
    if p.suffix.lower() not in (".jpg", ".jpeg") or not p.is_file():
        continue
    size = p.stat().st_size
    if size < a.min_kb * 1024:
        skip += 1
        continue
    try:
        im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
        im.thumbnail((a.max, a.max), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=a.quality, optimize=True, progressive=True)
    except Exception as e:
        print(f"! {p}: {e}")
        continue
    out = buf.tell()
    if out >= size:
        skip += 1
        continue
    tot_in += size
    tot_out += out
    n += 1
    print(f"{p}  {size/1024:.0f} KB -> {out/1024:.0f} KB")
    if not a.dry_run:
        p.write_bytes(buf.getvalue())

print(f"\n{n} foto {'(simulazione) ' if a.dry_run else ''}ottimizzate, {skip} saltate. "
      f"{tot_in/1e6:.1f} MB -> {tot_out/1e6:.1f} MB (risparmio {100*(1-tot_out/tot_in) if tot_in else 0:.0f}%)")
