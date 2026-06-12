# -*- coding: utf-8 -*-
"""
pdf_to_md.py — Rekursiv PDFs aus zvg_duss_koeln/ einlesen,
              nach zvg_id gruppieren, Text extrahieren, Markdown erzeugen.
"""
import re
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    print("[FEHLER] pypdf nicht installiert. Ausfuehren: pip install pypdf")
    sys.exit(1)

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from PIL import Image
except ImportError:
    Image = None

# PDFs groesser als LIMIT_KB werden beim Text-extrahieren auf max_chars gekuerzt
LIMIT_KB = 500
MAX_CHARS = 60000


def parse_filename(path: Path) -> dict | None:
    """
    NNN_label_file_XXXXX.pdf  ->  {index, label, file_id}
    """
    m = re.match(r"^(\d+)_(.+)_file_(\d+)\.pdf$", path.name, re.IGNORECASE)
    if not m:
        return None
    return {
        "index": int(m.group(1)),
        "label": m.group(2).lower().strip(),
        "file_id": m.group(3),
    }


def extract_text(path: Path) -> str:
    """Extrahiert Text aus PDF. Kuerzt bei grossen Dateien."""
    try:
        reader = PdfReader(str(path))
        pages = []
        total = 0
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
                pages.append(text)
                total += len(text)
                if total > MAX_CHARS:
                    pages.append("\n\n[... Text gekuerzt wegen Laenge ...]")
                    break
            except Exception as e:
                pages.append(f"[FEHLER SEITE {i+1}: {e}]")
        return "\n".join(pages).strip()
    except Exception as e:
        return f"[LESEFEHLER: {e}]"


def collect_pdfs(root_dir: Path) -> dict[str, dict]:
    """
    Durchsucht rekursiv nach PDFs, gruppiert nach zvg_id (aus Ordnername).
    Returns: {zvg_id: {bekanntmachung: Path|None, gutachten: Path|None, fotos: [Path], sonstige: [Path]}}
    """
    objects: dict[str, dict] = {}

    for pdf_path in sorted(root_dir.rglob("*.pdf")):
        info = parse_filename(pdf_path)
        if not info:
            continue

        # zvg_id aus uebergeordnetem Ordnernamen: zvg_168019
        zvg_id = None
        for parent in pdf_path.parents:
            m = re.match(r"^zvg_(\d+)$", parent.name)
            if m:
                zvg_id = m.group(1)
                break
        if not zvg_id:
            zvg_id = f"unknown_{info['file_id']}"

        if zvg_id not in objects:
            objects[zvg_id] = {
                "bekanntmachung": None,
                "gutachten": None,
                "fotos": [],
                "sonstige": [],
                "gericht": "",
            }

        # Gericht aus Pfad
        for parent in pdf_path.parents:
            if parent.name not in ("zvg_duss_koeln", "") and not parent.name.startswith("zvg_"):
                objects[zvg_id]["gericht"] = parent.name
                break

        label = info["label"]
        if "bekanntmachung" in label:
            objects[zvg_id]["bekanntmachung"] = pdf_path
        elif "gutachten" in label:
            objects[zvg_id]["gutachten"] = pdf_path
        elif "foto" in label:
            objects[zvg_id]["fotos"].append(pdf_path)
        else:
            objects[zvg_id]["sonstige"].append(pdf_path)

    return objects


MIN_IMG_KB = 8   # Kleinere Bilder ignorieren (Logos, Icons)
MAX_IMGS = 25     # Max Bilder pro Gutachten
THUMB_W = 320     # Thumbnail-Breite in px


def make_thumb(src: Path, thumb_dir: Path) -> Path | None:
    """Erzeugt Thumbnail mit max Breite THUMB_W."""
    if not Image:
        return None
    thumb_dir.mkdir(parents=True, exist_ok=True)
    out = thumb_dir / src.name
    if out.exists():
        return out
    try:
        img = Image.open(str(src))
        w, h = img.size
        if w > THUMB_W:
            ratio = THUMB_W / w
            img = img.resize((THUMB_W, int(h * ratio)), Image.LANCZOS)
        img.save(str(out), quality=80)
        return out
    except Exception:
        return None


def extract_images_from_pdf(pdf_path: Path, photos_dir: Path, obj_id: str, prefix: str = "img") -> list[str]:
    """Extrahiert eingebettete Bilder aus PDF (z.B. Gutachten). Ueberspringt kleine (< MIN_IMG_KB)."""
    if not fitz:
        return []
    photos_dir.mkdir(parents=True, exist_ok=True)
    rel_paths = []
    try:
        doc = fitz.open(str(pdf_path))
        img_idx = 0
        for page_num in range(len(doc)):
            if img_idx >= MAX_IMGS:
                break
            for img in doc[page_num].get_images(full=True):
                if img_idx >= MAX_IMGS:
                    break
                xref = img[0]
                try:
                    base = doc.extract_image(xref)
                    img_bytes = base["image"]
                    if len(img_bytes) < MIN_IMG_KB * 1024:
                        continue
                    ext = base["ext"]
                    img_idx += 1
                    out_name = f"{obj_id}_{prefix}{img_idx}.{ext}"
                    out_path = photos_dir / out_name
                    if out_path.exists():
                        rel_paths.append(f"photos/{out_name}")
                        continue
                    out_path.write_bytes(img_bytes)
                    rel_paths.append(f"photos/{out_name}")
                except Exception:
                    continue
        doc.close()
    except Exception as e:
        print(f"    [IMG-EXTRACT] {pdf_path.name}: {e}")
    return rel_paths


def render_page_as_jpg(pdf_paths: list[Path], photos_dir: Path, obj_id: str) -> list[str]:
    """Rendert erste Seite jedes Foto-PDF als JPG (fuer separate Fotos-PDFs)."""
    if not fitz:
        return []
    photos_dir.mkdir(parents=True, exist_ok=True)
    rel_paths = []
    for i, fp in enumerate(pdf_paths, 1):
        out_name = f"{obj_id}_foto{i}.jpg"
        out_path = photos_dir / out_name
        if out_path.exists():
            rel_paths.append(f"photos/{out_name}")
            continue
        try:
            doc = fitz.open(str(fp))
            pix = doc[0].get_pixmap(dpi=150)
            pix.save(str(out_path))
            doc.close()
            rel_paths.append(f"photos/{out_name}")
        except Exception as e:
            print(f"    [FOTO-FEHLER] {fp.name}: {e}")
    return rel_paths


def build_markdown(zvg_id: str, docs: dict, photo_refs: list[str] | None = None) -> str:
    b = docs["bekanntmachung"]
    g = docs["gutachten"]
    fotos = docs["fotos"]
    sonstige = docs["sonstige"]
    gericht = docs.get("gericht", "")

    # ID = file_id der Bekanntmachung (falls vorhanden), sonst Gutachten
    obj_id = zvg_id
    if b:
        info = parse_filename(b)
        if info:
            obj_id = info["file_id"]
    elif g:
        info = parse_filename(g)
        if info:
            obj_id = info["file_id"]

    lines = []
    lines.append(f"# Objekt {obj_id}")
    lines.append("")
    lines.append(f"zvg_id: {zvg_id}  ")
    if gericht:
        lines.append(f"Gericht: {gericht}  ")
    lines.append("")
    lines.append("## Quellen")
    lines.append(f"- Beschluss: {'vorhanden' if b else 'nicht vorhanden'}")
    lines.append(f"- Gutachten: {'vorhanden' if g else 'nicht vorhanden'}")
    lines.append(f"- Fotos: {len(fotos)} Dateien")
    lines.append(f"- Sonstige: {len(sonstige)} Dateien")
    lines.append("")

    # --- Beschluss ---
    lines.append("## Beschluss (Rohtext)")
    lines.append("")
    if b:
        text = extract_text(b)
        lines.append(text if text else "[KEIN TEXT - moeglicherweise gescannt ohne OCR]")
    else:
        lines.append("nicht vorhanden")
    lines.append("")

    # --- Gutachten ---
    lines.append("## Gutachten (Rohtext)")
    lines.append("")
    if g:
        text = extract_text(g)
        lines.append(text if text else "[KEIN TEXT - moeglicherweise gescannt ohne OCR]")
    else:
        lines.append("nicht vorhanden")
    lines.append("")

    # --- Fotos ---
    if fotos and photo_refs:
        lines.append("## Fotos")
        lines.append("")
        for fp, ref in zip(fotos, photo_refs):
            kb = fp.stat().st_size / 1024
            lines.append(f"![Foto]({ref})")
            lines.append(f"*{fp.name} ({kb:.0f} KB)*")
            lines.append("")
    elif fotos:
        lines.append("## Fotos")
        lines.append("")
        for fp in fotos:
            kb = fp.stat().st_size / 1024
            lines.append(f"- {fp.name} ({kb:.0f} KB)")
        lines.append("")

    # --- Sonstige ---
    if sonstige:
        lines.append("## Sonstige Anhaenge")
        lines.append("")
        for sp in sonstige:
            kb = sp.stat().st_size / 1024
            info = parse_filename(sp)
            label = info["label"] if info else sp.stem
            lines.append(f"- {label}: {sp.name} ({kb:.0f} KB)")
        lines.append("")

    return "\n".join(lines)


def main():
    print("=" * 60)
    print("PDF zu Markdown Konverter (Zwangsversteigerung)")
    print("=" * 60)

    SOURCE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
    OUTPUT_DIR = SOURCE_DIR / "md"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Quellordner : {SOURCE_DIR}")
    print(f"Zielordner  : {OUTPUT_DIR}")
    print()

    objects = collect_pdfs(SOURCE_DIR)
    print(f"Gefundene Objekte (zvg_id): {len(objects)}")
    print()

    pairs = 0
    singles = 0
    errors = 0

    for zvg_id, docs in sorted(objects.items()):
        b = docs["bekanntmachung"]
        g = docs["gutachten"]
        fotos = docs["fotos"]

        # ID aus Bekanntmachung
        obj_id = zvg_id
        if b:
            info = parse_filename(b)
            if info:
                obj_id = info["file_id"]

        if b and g:
            pairs += 1
            status = f"Beschluss + Gutachten + {len(fotos)} Fotos"
        elif b:
            singles += 1
            status = f"nur Beschluss + {len(fotos)} Fotos"
        elif g:
            singles += 1
            status = f"nur Gutachten + {len(fotos)} Fotos"
        else:
            errors += 1
            print(f"  [FEHLER] zvg_id={zvg_id}: weder Beschluss noch Gutachten")
            continue

        print(f"  ID {obj_id} (zvg={zvg_id}): {status}")

        # Fotos: aus Gutachten extrahieren + separate Fotos-PDFs rendern
        photos_dir = OUTPUT_DIR / "photos"
        thumb_dir = OUTPUT_DIR / "photos" / "thmb"
        photo_refs = []
        if g:
            extracted = extract_images_from_pdf(g, photos_dir, obj_id, prefix="img")
            for p_rel in extracted:
                make_thumb(photos_dir / Path(p_rel).name, thumb_dir)
            photo_refs.extend(extracted)
            if extracted:
                print(f"    -> {len(extracted)} Bilder aus Gutachten extrahiert")
        if fotos:
            rendered = render_page_as_jpg(fotos, photos_dir, obj_id)
            for p_rel in rendered:
                make_thumb(photos_dir / Path(p_rel).name, thumb_dir)
            photo_refs.extend(rendered)

        md_text = build_markdown(zvg_id, docs, photo_refs if photo_refs else None)
        out_path = OUTPUT_DIR / f"{obj_id}.md"
        try:
            out_path.write_text(md_text, encoding="utf-8")
            print(f"    -> {out_path.name}")
        except Exception as e:
            errors += 1
            print(f"    [SCHREIBFEHLER] {out_path.name}: {e}")

    print()
    print("=" * 60)
    print("Ergebnis:")
    print(f"  Paare (Beschluss + Gutachten): {pairs}")
    print(f"  Einzelne Objekte:              {singles}")
    print(f"  Fehler:                        {errors}")
    print(f"  Markdown-Dateien:              {pairs + singles}")
    print(f"  Ordner: {OUTPUT_DIR.absolute()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
