#!/usr/bin/env python3
"""
app.py — Gradio WebUI для ZVG-Pipeline.
         Codespaces-ready: Linux, kein .bat, alles in einem Tab.
"""
import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import gradio as gr
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = "https://www.zvg-portal.de/index.php"
OUT_DIR = Path("output")
MD_DIR = OUT_DIR / "md"
PHOTOS_DIR = MD_DIR / "photos"
THUMB_DIR = PHOTOS_DIR / "thmb"
BUILD_DIR = Path("build")
CACHE = Path("analysis_cache.json")

NRW_COURTS = [
    "Aachen", "Arnsberg", "Bielefeld", "Bochum", "Bonn", "Borken",
    "Bottrop", "Detmold", "Dortmund", "Duesseldorf", "Duisburg", "Essen",
    "Euskirchen", "Gelsenkirchen", "Guetersloh", "Hagen", "Hamm",
    "Hattingen", "Herford", "Iserlohn", "Kleve", "Koeln", "Krefeld",
    "Lemgo", "Lippstadt", "Luedenscheid", "Moenchengladbach", "Muenster",
    "Neuss", "Paderborn", "Recklinghausen", "Siegburg", "Siegen",
    "Soest", "Solingen", "Unna", "Viersen", "Wuppertal",
]

ENV = {}
env_path = Path(__file__).with_name(".env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            ENV[k.strip()] = v.strip().strip('"').strip("'")

DEEPSEEK_KEY = ENV.get("DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
GITHUB_TOKEN = ENV.get("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
GITHUB_REPO = "igalvadim-debug/Analitik"

# ---------------------------------------------------------------------------
# Helpers (from immo.py)
# ---------------------------------------------------------------------------
def safe_name(s: str) -> str:
    s = s.replace("\xa0", " ").replace("&nbsp;", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return re.sub(r'[<>:"/\\\\|?*]+', "_", s).strip(" ._")


def get_qs_value(url: str, key: str, default: str = "") -> str:
    from urllib.parse import parse_qs, urlparse
    try:
        return parse_qs(urlparse(url).query).get(key, [default])[0]
    except Exception:
        return default


def normalize_gericht(name: str) -> str:
    for a, u in [("ae", "ä"), ("oe", "ö"), ("ue", "ü"), ("Ae", "Ä"), ("Oe", "Ö"), ("Ue", "Ü"), ("ss", "ß")]:
        name = name.replace(a, u)
    return name


# ---------------------------------------------------------------------------
# Pipeline steps (called sequentially)
# ---------------------------------------------------------------------------
async def step_download(gerichte: list[str], progress=gr.Progress()) -> str:
    """Download PDFs via Playwright."""
    from playwright.async_api import async_playwright

    OUT_DIR.mkdir(exist_ok=True)
    total_downloaded = 0
    log = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        for gericht in gerichte:
            progress_msg = f"[{gericht}] Suche..."
            log.append(progress_msg)
            progress(progress_msg)

            gericht_norm = normalize_gericht(gericht)
            await page.goto(f"{BASE}?button=Termine%20suchen")
            await page.wait_for_load_state("networkidle")
            await page.evaluate("""() => {
                const sel = document.querySelector('select[name="land_abk"]');
                sel.value = 'nw';
                sel.dispatchEvent(new Event('change', {bubbles:true}));
            }""")
            await page.wait_for_timeout(2500)

            ger_options = await page.evaluate("""() =>
                [...document.querySelector('select[name="ger_id"]').options].map(o => ({
                    value: o.value, text: (o.textContent||'').trim()
                }))
            """)
            ger_id = None
            for opt in ger_options:
                if gericht_norm.lower() in normalize_gericht(opt["text"].lower()):
                    ger_id = opt["value"]
                    break
            if not ger_id:
                log.append(f"  [SKIP] ger_id nicht gefunden fuer {gericht}")
                continue

            log.append(f"  ger_id={ger_id}")
            await page.evaluate(f"""(v) => {{
                const sel = document.querySelector('select[name="ger_id"]');
                sel.value = v; sel.dispatchEvent(new Event('change',{{bubbles:true}}));
            }}""", ger_id)
            await page.wait_for_load_state("networkidle")
            await page.click("input[value='Suchen']")
            await page.wait_for_timeout(1200)

            try:
                selects = await page.locator("select").all()
                for sel in selects:
                    texts = [t.strip().lower() for t in await sel.locator("option").all_text_contents()]
                    if "alle" in texts:
                        await sel.select_option(label="alle")
                        await page.wait_for_load_state("networkidle")
                        await page.wait_for_timeout(1200)
                        break
            except Exception:
                pass

            # --- Collect links from search page ---
            search_url = page.url
            search_links = await page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*="button=showAnhang"]');
                return [...links].map(a => ({text: (a.textContent||'').trim(), href: a.href||''}));
            }""")

            detail_links = await page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*="button=showZvg"]');
                return [...links].map(a => ({href: a.href||''}));
            }""")

            # Collect items from search page
            seen = set()
            all_items = []
            for item in search_links:
                file_id = get_qs_value(item["href"], "file_id")
                zvg_id = get_qs_value(item["href"], "zvg_id")
                if not file_id or (file_id, zvg_id) in seen:
                    continue
                seen.add((file_id, zvg_id))
                lt = item["text"].lower()
                label = ("gutachten" if "gutachten" in lt else
                         "bekanntmachung" if "bekanntmachung" in lt else
                         "expose" if "expos" in lt else "anhang")
                all_items.append({"gericht": gericht, "file_id": file_id, "zvg_id": zvg_id, "label": label, "text": item["text"], "url": item["href"]})

            log.append(f"  PDF vom Such-Ergebnis: {len(all_items)}")

            # Collect zvg_ids
            zvg_ids = set()
            for d in detail_links:
                zvg_id = get_qs_value(d["href"], "zvg_id")
                if zvg_id:
                    zvg_ids.add(zvg_id)
            for it in all_items:
                if it["zvg_id"]:
                    zvg_ids.add(it["zvg_id"])

            # Visit each detail page
            for zvg_id in sorted(zvg_ids):
                detail_url = f"{BASE}?button=showZvg&land_abk=nw&zvg_id={zvg_id}"
                try:
                    await page.goto(detail_url, timeout=15000, referer=search_url)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(600)
                    detail_links_js = await page.evaluate("""() => {
                        const links = document.querySelectorAll('a[href*="button=showAnhang"]');
                        return [...links].map(a => ({text: (a.textContent||'').trim(), href: a.href||''}));
                    }""")
                    before = len(all_items)
                    for item in detail_links_js:
                        file_id = get_qs_value(item["href"], "file_id")
                        zvid = get_qs_value(item["href"], "zvg_id")
                        if not file_id or (file_id, zvid) in seen:
                            continue
                        seen.add((file_id, zvid))
                        lt = item["text"].lower()
                        label = ("gutachten" if "gutachten" in lt else
                                 "bekanntmachung" if "bekanntmachung" in lt else
                                 "expose" if "expos" in lt else "anhang")
                        all_items.append({"gericht": gericht, "file_id": file_id, "zvg_id": zvid, "label": label, "text": item["text"], "url": item["href"]})
                except Exception as e:
                    log.append(f"  zvg_id={zvg_id} ERROR: {e}")

            log.append(f"  Total links: {len(all_items)}")

            # Download
            for i, item in enumerate(all_items, 1):
                zvg_id = item["zvg_id"] or "unknown"
                file_id = item["file_id"]
                label = item["label"]
                folder = OUT_DIR / gericht / f"zvg_{zvg_id}"
                folder.mkdir(parents=True, exist_ok=True)
                fname = folder / safe_name(f"{i:03d}_{label}_file_{file_id}.pdf")
                try:
                    resp = await context.request.get(item["url"], headers={
                        "Referer": search_url, "Accept": "application/pdf,*/*"
                    })
                    body = await resp.body()
                    if body[:4] == b"%PDF":
                        fname.write_bytes(body)
                        total_downloaded += 1
                    else:
                        log.append(f"  SKIP: {file_id} not PDF (HTTP {resp.status})")
                except Exception as e:
                    log.append(f"  ERROR: {file_id} {e}")
                await asyncio.sleep(0.1)
            progress(f"[{gericht}] done: {len(all_items)} links")

        await browser.close()

    log.append(f"\nDownloaded: {total_downloaded} PDFs")
    return "\n".join(log)


def step_convert_md(progress=gr.Progress()) -> str:
    """Convert PDFs to Markdown + extract images."""
    try:
        from pypdf import PdfReader
        import fitz
        from PIL import Image
    except ImportError as e:
        return f"ERROR: {e}. Run: pip install pypdf PyMuPDF Pillow"

    log = []
    MD_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    # Collect PDFs
    objects = {}
    for pdf_path in sorted(OUT_DIR.rglob("*.pdf")):
        m = re.match(r"^(\d+)_(.+)_file_(\d+)\.pdf$", pdf_path.name, re.IGNORECASE)
        if not m:
            continue
        label = m.group(2).lower()
        zvg_id = None
        for parent in pdf_path.parents:
            zm = re.match(r"^zvg_(\d+)$", parent.name)
            if zm:
                zvg_id = zm.group(1)
                break
        if not zvg_id:
            zvg_id = f"unknown_{m.group(3)}"
        gericht = ""
        for parent in pdf_path.parents:
            if parent.name not in ("output", "") and not parent.name.startswith("zvg_"):
                gericht = parent.name
                break
        if zvg_id not in objects:
            objects[zvg_id] = {"b": None, "g": None, "gericht": gericht}
        if "bekanntmachung" in label:
            objects[zvg_id]["b"] = pdf_path
        elif "gutachten" in label:
            objects[zvg_id]["g"] = pdf_path

    log.append(f"Objects: {len(objects)}")
    progress(f"Objects: {len(objects)}")

    total = len(objects)
    for idx, (zvg_id, docs) in enumerate(sorted(objects.items())):
        b, g = docs["b"], docs["g"]
        if not b and not g:
            continue
        obj_id = zvg_id
        if b:
            m = re.match(r"^\d+_(.+)_file_(\d+)\.pdf$", b.name)
            if m:
                obj_id = m.group(2)
        elif g:
            m = re.match(r"^\d+_(.+)_file_(\d+)\.pdf$", g.name)
            if m:
                obj_id = m.group(2)

        progress(f"[{idx+1}/{total}] ID={obj_id}")

        # Extract text
        def read_pdf(path):
            try:
                reader = PdfReader(str(path))
                parts = []
                for page in reader.pages:
                    try:
                        t = page.extract_text() or ""
                        parts.append(t)
                    except Exception:
                        pass
                return "\n".join(parts).strip()
            except Exception:
                return "[LESEFEHLER]"

        b_text = read_pdf(b) if b else "nicht vorhanden"
        g_text = read_pdf(g) if g else "nicht vorhanden"

        # Extract images from Gutachten
        photo_refs = []
        if g:
            try:
                doc = fitz.open(str(g))
                img_idx = 0
                for pn in range(len(doc)):
                    if img_idx >= 25:
                        break
                    for img in doc[pn].get_images(full=True):
                        if img_idx >= 25:
                            break
                        try:
                            base = doc.extract_image(img[0])
                            img_bytes = base["image"]
                            if len(img_bytes) < 8 * 1024:
                                continue
                            ext = base["ext"]
                            img_idx += 1
                            oname = f"{obj_id}_img{img_idx}.{ext}"
                            (PHOTOS_DIR / oname).write_bytes(img_bytes)
                            # Thumbnail
                            try:
                                im = Image.open(str(PHOTOS_DIR / oname))
                                w, h = im.size
                                if w > 320:
                                    im = im.resize((320, int(h * 320 / w)), Image.LANCZOS)
                                im.save(str(THUMB_DIR / oname), quality=80)
                            except Exception:
                                pass
                            photo_refs.append(oname)
                        except Exception:
                            continue
                doc.close()
            except Exception as e:
                log.append(f"  IMG-ERR {obj_id}: {e}")

        # Build MD
        lines = [f"# Objekt {obj_id}", "", f"zvg_id: {zvg_id}", f"Gericht: {docs['gericht']}", "",
                 "## Quellen", f"- Beschluss: {'vorhanden' if b else 'nicht vorhanden'}",
                 f"- Gutachten: {'vorhanden' if g else 'nicht vorhanden'}",
                 f"- Fotos: {len(photo_refs)}", "",
                 "## Beschluss (Rohtext)", "", b_text, "",
                 "## Gutachten (Rohtext)", "", g_text, ""]
        if photo_refs:
            lines.append("## Fotos")
            lines.append("")
            for ref in photo_refs:
                lines.append(f"![Foto](photos/{ref})")
            lines.append("")
        (MD_DIR / f"{obj_id}.md").write_text("\n".join(lines), encoding="utf-8")
        log.append(f"  {obj_id}: {'B+G' if b and g else 'B' if b else 'G'} +{len(photo_refs)} imgs")

    log.append(f"\nMD files: {len(list(MD_DIR.glob('*.md')))}")
    log.append(f"Photos: {len(list(PHOTOS_DIR.glob('*.jpg')))}")
    return "\n".join(log)


def step_build_html(do_upload: bool = False, progress=gr.Progress()) -> str:
    """Analyze MDs via DeepSeek → build/index.html → optional GitHub upload."""
    if not DEEPSEEK_KEY:
        return "ERROR: DEEPSEEK_API_KEY not set in .env or env var."

    log = []
    md_files = sorted(MD_DIR.glob("*.md"), key=lambda p: p.stem)
    log.append(f"MD files: {len(md_files)}")

    cache = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        log.append(f"Cache: {len(cache)} entries")

    results = []
    system_prompt = "Du bist eine JSON-API. Antworte AUSSCHLIESSLICH mit gueltigem JSON. Kein Markdown, kein ```json, kein Kommentar. Nur {...}."
    user_prompt_tmpl = """Analysiere den Zwangsversteigerungs-Text (Beschluss + Gutachten).
Erstelle exakt dieses JSON mit ALLEN Feldern:
{"id":"<file_id>","addr":"<Adresse>","bezirk":"<Stadtteil>","type":"Wohnung|Haus|Gewerbe|Erbbaurecht|Lager|Garage|Stellplatz|Sonstiges","wfl":<m²|null>,"rooms":<Zahl|null>,"bj":<Jahr|null>,"vw":<EUR|null>,"date":"<YYYY-MM-DD|unbekannt>","beschluss":true/false,"gutachten":true/false,"pros":["Vorteil1","Vorteil2","Vorteil3"],"cons":["Nachteil1","Nachteil2","Nachteil3"],"risks":"<1 Satz>","risk":"low|med|high","markt":"<Spanne>","discount":"<Abschlag>","liqui":"hoch|mittel|niedrig|sehr niedrig","miete":"<Spanne/Mo>","yld":"<Rendite>","costs":"<Sanierung>","verdict":"<Kaufempfehlung mit Max-Gebot>","bid":"<Max Gebot>"}
TEXT:
"""

    total = len(md_files)
    for i, md_path in enumerate(md_files, 1):
        obj_id = md_path.stem
        progress(f"[{i}/{total}] {obj_id}")

        if obj_id in cache:
            results.append(cache[obj_id])
            continue

        text = md_path.read_text(encoding="utf-8")
        if len(text) > 24000:
            text = text[:18000] + "\n\n[...]\n\n" + text[-6000:]

        data = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                    json={"model": "deepseek-chat", "temperature": 0.3, "max_tokens": 2048,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": system_prompt},
                                       {"role": "user", "content": user_prompt_tmpl + text}]},
                    timeout=120)
                if resp.status_code == 200:
                    raw = resp.json()["choices"][0]["message"]["content"]
                    raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
                    s = raw.find("{")
                    e = raw.rfind("}")
                    if s != -1 and e != -1:
                        data = json.loads(raw[s:e+1])
                        data["id"] = str(data.get("id", obj_id))
                        break
                time.sleep(2)
            except Exception as e:
                log.append(f"  {obj_id}: req err {e}")
                time.sleep(2)

        if not data:
            data = {"id": obj_id, "addr": f"Objekt {obj_id}", "bezirk": "", "type": "Sonstiges",
                    "wfl": None, "rooms": None, "bj": None, "vw": None, "date": "unbekannt",
                    "beschluss": False, "gutachten": False, "pros": [], "cons": [],
                    "risks": "Analyse fehlgeschlagen", "risk": "med", "markt": "?", "discount": "?",
                    "liqui": "?", "miete": "?", "yld": "?", "costs": "?", "verdict": "N/A", "bid": "?"}

        # zvg_link + photos
        m = re.search(r"zvg_id:\s*(\d+)", md_path.read_text(encoding="utf-8"))
        zvg_id = m.group(1) if m else obj_id
        data["zvg_link"] = f"https://www.zvg-portal.de/index.php?button=showZvg&zvg_id={zvg_id}&land_abk=nw"

        thmb_files = sorted(THUMB_DIR.glob(f"{obj_id}_img*.*")) + sorted(THUMB_DIR.glob(f"{obj_id}_foto*.*"))
        data["photos"] = [f"pic/thmb/{p.name}" for p in thmb_files]

        results.append(data)
        cache[obj_id] = data
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        log.append(f"  {obj_id}: {data.get('addr','?')} | {data.get('type','?')} | {data.get('vw','?')}")

    # Generate HTML
    items_json = json.dumps(results, ensure_ascii=False, indent=2)
    n = len(results)
    html = generate_html(n, items_json)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    (BUILD_DIR / "index.html").write_text(html, encoding="utf-8")
    log.append(f"\nHTML: {BUILD_DIR.absolute()}/index.html")

    # Copy photos
    pic_dir = BUILD_DIR / "pic"
    thmb_build = pic_dir / "thmb"
    pic_dir.mkdir(parents=True, exist_ok=True)
    thmb_build.mkdir(parents=True, exist_ok=True)
    for f in PHOTOS_DIR.glob("*"):
        if f.is_file():
            shutil.copy2(f, pic_dir / f.name)
    for f in THUMB_DIR.glob("*"):
        if f.is_file():
            shutil.copy2(f, thmb_build / f.name)
    log.append(f"Photos: {len(list(pic_dir.glob('*')))} in build/pic/")

    # GitHub upload
    if do_upload and GITHUB_TOKEN:
        log.append("GitHub upload...")
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        def gh_upload(path_in_repo, local_path):
            content_b64 = __import__("base64").b64encode(local_path.read_bytes()).decode()
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path_in_repo}"
            sha = None
            try:
                r = requests.get(api_url, headers=headers, timeout=10)
                if r.status_code == 200:
                    sha = r.json().get("sha")
            except Exception:
                pass
            body = {"message": "update", "content": content_b64, "branch": "main"}
            if sha:
                body["sha"] = sha
            return requests.put(api_url, json=body, headers=headers, timeout=30).status_code in (200, 201)

        # Clear old pics
        try:
            r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/build/pic", headers=headers, timeout=15)
            if r.status_code == 200:
                for item in r.json():
                    if item.get("type") == "file":
                        requests.delete(f"https://api.github.com/repos/{GITHUB_REPO}/contents/build/pic/{item['name']}",
                                        json={"message": "clear", "sha": item["sha"], "branch": "main"}, headers=headers, timeout=15)
        except Exception:
            pass

        gh_upload("build/index.html", BUILD_DIR / "index.html")
        for f in pic_dir.glob("*"):
            if f.is_file():
                gh_upload(f"build/pic/{f.name}", f)
        for f in thmb_build.glob("*"):
            if f.is_file():
                gh_upload(f"build/pic/thmb/{f.name}", f)
        log.append(f"GitHub: https://github.com/{GITHUB_REPO}/blob/main/build/index.html")

    log.append("DONE!")
    return "\n".join(log)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
def generate_html(n, items_json):
    return f'''<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Zwangsversteigerung — {n} Objekte</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f2;color:#1a1a18;padding:1.5rem}}
.header{{padding-bottom:1rem;border-bottom:1px solid #e0dfd8;margin-bottom:1.5rem}}
.header h1{{font-size:22px;font-weight:500}}
.header p{{font-size:14px;color:#888780;margin-top:4px}}
.filters{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:1.25rem;align-items:center}}
.filters select,.filters input{{font-size:13px;padding:6px 10px;border:1px solid #d3d1c7;border-radius:8px;background:#fff;color:#1a1a18}}
.filters label{{font-size:13px;color:#5f5e5a}}
.stats-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.5rem}}
.stat{{background:#ededea;border-radius:8px;padding:12px 14px}}
.stat-label{{font-size:12px;color:#888780;margin-bottom:4px}}
.stat-val{{font-size:20px;font-weight:500}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}}
.card{{background:#fff;border:1px solid #e0dfd8;border-radius:12px;padding:1rem 1.125rem;transition:border-color .15s}}
.card:hover{{border-color:#b4b2a9}}
.card-head{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}}
.card-id{{font-size:12px;color:#888780}}
.card-type{{font-size:11px;padding:2px 8px;border-radius:8px;font-weight:500}}
.card-addr{{font-size:15px;font-weight:500;margin-bottom:4px}}
.card-sub{{font-size:13px;color:#888780;margin-bottom:10px}}
.card-photos{{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}}
.card-photos img{{width:56px;height:42px;object-fit:cover;border-radius:6px;cursor:pointer;border:1px solid #e0dfd8;transition:transform .15s}}
.card-photos img:hover{{transform:scale(2.2);z-index:10;border-color:#b4b2a9}}
.card-price{{font-size:22px;font-weight:500;margin-bottom:8px}}
.risk-row{{display:flex;gap:6px;margin-bottom:10px;align-items:center;flex-wrap:wrap}}
.risk-badge{{font-size:11px;padding:3px 9px;border-radius:8px;font-weight:500}}
.risk-low{{background:#eaf3de;color:#3b6d11}}
.risk-med{{background:#faeeda;color:#854f0b}}
.risk-high{{background:#fcebeb;color:#a32d2d}}
.fin-block{{background:#f5f5f2;border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:12px}}
.fin-row{{display:flex;justify-content:space-between;margin-bottom:4px}}
.fin-row:last-child{{margin-bottom:0}}
.fin-label{{color:#888780}}
.verdict{{font-size:12px;color:#5f5e5a;line-height:1.5;border-top:1px solid #e0dfd8;padding-top:8px}}
.sources{{display:flex;gap:6px;margin-top:8px}}
.src-yes{{background:#e1f5ee;color:#0f6e56;font-size:10px;padding:2px 7px;border-radius:10px;font-weight:500}}
.src-no{{background:#f1efe8;color:#5f5e5a;font-size:10px;padding:2px 7px;border-radius:10px}}
.zvg-link{{display:inline-block;margin-top:6px;font-size:11px;color:#185fa5;text-decoration:none;border:1px solid #d3d1c7;padding:3px 10px;border-radius:6px}}
.zvg-link:hover{{background:#e6f1fb;border-color:#185fa5}}
.date-badge{{font-size:11px;color:#5f5e5a;background:#f1efe8;padding:2px 8px;border-radius:8px}}
.no-results{{text-align:center;padding:3rem;color:#888780;font-size:14px}}
.pros-cons{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}}
.pc-label{{font-size:11px;color:#888780;margin-bottom:4px;font-weight:500;text-transform:uppercase;letter-spacing:.04em}}
.pc-list{{list-style:none;padding:0}}
.pc-list li{{font-size:12px;color:#5f5e5a;margin-bottom:3px}}
@media(max-width:600px){{.stats-row{{grid-template-columns:repeat(2,1fr)}}.grid{{grid-template-columns:1fr}}.pros-cons{{grid-template-columns:1fr}}}}
</style></head><body>
<div class="header"><h1>Zwangsversteigerung &mdash; {n} Objekte</h1><p>Analyse per DeepSeek &middot; {n} Objekte</p></div>
<div class="stats-row"><div class="stat"><div class="stat-label">Objekte gesamt</div><div class="stat-val" id="s-total">{n}</div></div><div class="stat"><div class="stat-label">Min. Verkehrswert</div><div class="stat-val" id="s-min">&mdash;</div></div><div class="stat"><div class="stat-label">Max. Verkehrswert</div><div class="stat-val" id="s-max">&mdash;</div></div><div class="stat"><div class="stat-label">Naechster Termin</div><div class="stat-val" id="s-next">&mdash;</div></div></div>
<div class="filters"><label>Typ:</label><select id="f-type" onchange="render()"><option value="">Alle</option><option value="Wohnung">Wohnung</option><option value="Haus">Haus</option><option value="Gewerbe">Gewerbe</option><option value="Erbbaurecht">Erbbaurecht</option><option value="Lager">Lager</option></select><label>Risiko:</label><select id="f-risk" onchange="render()"><option value="">Alle</option><option value="low">Niedrig</option><option value="med">Mittel</option><option value="high">Hoch</option></select><label>Sort:</label><select id="f-sort" onchange="render()"><option value="date">Datum</option><option value="asc">Preis &uarr;</option><option value="desc">Preis &darr;</option></select><input type="text" id="f-search" placeholder="Suche Adresse..." oninput="render()" style="min-width:160px"></div>
<div class="grid" id="grid"></div><div class="no-results" id="no-results" style="display:none">Keine Ergebnisse</div>
<script>const DATA={items_json};const typeColor={{"Wohnung":"background:#e6f1fb;color:#185fa5","Haus":"background:#eaf3de;color:#3b6d11","Gewerbe":"background:#faeeda;color:#854f0b","Erbbaurecht":"background:#fbeaf0;color:#993556","Lager":"background:#f1efe8;color:#5f5e5a","Garage":"background:#f1efe8;color:#5f5e5a","Stellplatz":"background:#f1efe8;color:#5f5e5a","Sonstiges":"background:#f1efe8;color:#5f5e5a"}};const riskLabel={{"low":"Niedriges Risiko","med":"Mittleres Risiko","high":"Hohes Risiko"}};function fmtPrice(v){{if(!v)return"\u2014";if(v>=1e6)return(v/1e6).toFixed(2).replace(/\\.?0+$/,"")+" Mio EUR";return(v/1e3).toFixed(0)+" kEUR"}}function render(){{let data=[...DATA];const tf=document.getElementById("f-type").value,rf=document.getElementById("f-risk").value,sf=document.getElementById("f-sort").value,search=document.getElementById("f-search").value.toLowerCase();if(tf)data=data.filter(d=>d.type===tf);if(rf)data=data.filter(d=>d.risk===rf);if(search)data=data.filter(d=>(d.addr||"").toLowerCase().includes(search)||(d.bezirk||"").toLowerCase().includes(search));if(sf==="asc")data.sort((a,b)=>(a.vw||0)-(b.vw||0));else if(sf==="desc")data.sort((a,b)=>(b.vw||0)-(a.vw||0));else data.sort((a,b)=>(a.date||"z").localeCompare(b.date||"z"));document.getElementById("grid").innerHTML=data.map(d=>`<div class="card"><div class="card-head"><span class="card-id">#${{d.id}}</span><span class="card-type" style="${{typeColor[d.type]||typeColor["Sonstiges"]}}">${{d.type||"?"}}</span></div><div class="card-addr">${{d.addr||"Unbekannt"}}</div><div class="card-sub">${{d.bezirk||""}}${{d.wfl?" \\u00b7 "+d.wfl+" m\\u00b2":""}}${{d.rooms?" \\u00b7 "+d.rooms+" Zi":""}}${{d.bj?" \\u00b7 BJ "+d.bj:""}}</div>${{(d.photos||[]).length?`<div class="card-photos">${{d.photos.map(p=>`<img src="${{p}}" alt="Foto" loading="lazy">`).join("")}}</div>`:""}}<div class="card-price">${{fmtPrice(d.vw)}} <span class="date-badge">${{d.date||"?"}}</span></div><div class="risk-row"><span class="risk-badge risk-${{d.risk||"med"}}">${{riskLabel[d.risk]||d.risk}}</span><span class="date-badge">${{d.discount||""}}</span></div>${{(d.pros||[]).length||(d.cons||[]).length?`<div class="pros-cons"><div><div class="pc-label">Pro</div><ul class="pc-list">${{(d.pros||[]).map(p=>`<li>+ ${{p}}</li>`).join("")}}</ul></div><div><div class="pc-label">Contra</div><ul class="pc-list">${{(d.cons||[]).map(c=>`<li>\\u2212 ${{c}}</li>`).join("")}}</ul></div></div>`:""}}<div class="fin-block"><div class="fin-row"><span class="fin-label">Marktpreis</span><span>${{d.markt||"\\u2014"}}</span></div><div class="fin-row"><span class="fin-label">Miete</span><span>${{d.miete||"\\u2014"}}</span></div><div class="fin-row"><span class="fin-label">Rendite</span><span>${{d.yld||"\\u2014"}}</span></div><div class="fin-row"><span class="fin-label">Liquiditaet</span><span>${{d.liqui||"\\u2014"}}</span></div><div class="fin-row"><span class="fin-label">Risiken</span><span style="text-align:right;max-width:180px">${{d.risks||"\\u2014"}}</span></div></div><div class="verdict">${{d.verdict||""}}</div><div class="sources"><span class="${{d.beschluss?"src-yes":"src-no"}}">Beschluss ${{d.beschluss?"OK":"\\u2014"}}</span><span class="${{d.gutachten?"src-yes":"src-no"}}">Gutachten ${{d.gutachten?"OK":"\\u2014"}}</span></div>${{d.zvg_link?`<a href="${{d.zvg_link}}" target="_blank" rel="noopener" class="zvg-link">ZVG-Portal &rarr;</a>`:""}}</div>`).join("");document.getElementById("no-results").style.display=data.length?"none":"block";const vws=data.map(d=>d.vw).filter(v=>v);if(vws.length){{document.getElementById("s-min").textContent=fmtPrice(Math.min(...vws));document.getElementById("s-max").textContent=fmtPrice(Math.max(...vws))}}const dates=data.map(d=>d.date).filter(d=>d&&d!=="unbekannt").sort();if(dates.length)document.getElementById("s-next").textContent=dates[0]}}render();</script></body></html>'''


# ---------------------------------------------------------------------------
# Gradio UI
# ---------------------------------------------------------------------------
def run_pipeline(gerichte_str: str, do_upload: bool, progress=gr.Progress()):
    gerichte = [g.strip() for g in gerichte_str.split(",") if g.strip()]
    if not gerichte:
        return "ERROR: Keine Gerichte ausgewaehlt."

    log = []
    log.append(f"Gerichte: {gerichte}")

    # Step 1: Download
    log.append("\n=== DOWNLOAD PDFs ===")
    log.append(asyncio.run(step_download(gerichte, progress)))

    # Step 2: Convert to MD
    log.append("\n=== PDF -> MD + FOTOS ===")
    log.append(step_convert_md(progress))

    # Step 3: Build HTML
    log.append("\n=== ANALYSE + HTML ===")
    log.append(step_build_html(do_upload, progress))

    return "\n".join(log)


with gr.Blocks(title="ZVG-Portal Pipeline") as demo:
    gr.Markdown("# 🏛️ ZVG-Portal Downloader & Analyzer")
    gr.Markdown("NRW Zwangsversteigerungen — PDF, Markdown, Fotos, HTML-Analyse per DeepSeek, GitHub Upload.")

    with gr.Row():
        courts = gr.Textbox(
            label="Gerichte (Komma-getrennt)",
            value="Duesseldorf, Koeln",
            placeholder="Duesseldorf, Koeln, Bottrop, Bonn...",
            lines=2,
        )
        upload_toggle = gr.Checkbox(label="Upload to GitHub", value=True)

    btn = gr.Button("🚀 Pipeline starten", variant="primary")
    output = gr.Textbox(label="Log", lines=25, max_lines=50)

    btn.click(run_pipeline, inputs=[courts, upload_toggle], outputs=output)

    gr.Markdown("---\n### Codespaces Setup\n```bash\npip install gradio playwright pypdf PyMuPDF Pillow requests\nplaywright install chromium\npython app.py\n```")

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
