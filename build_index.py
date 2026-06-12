#!/usr/bin/env python3
"""
build_index.py — Liest MD-Dateien, analysiert per DeepSeek API,
                 generiert index.html, pushed zu GitHub.
"""
import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config aus .env
# ---------------------------------------------------------------------------
ENV = {}
env_path = Path(__file__).with_name(".env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            ENV[key.strip()] = val.strip().strip('"').strip("'")

DEEPSEEK_KEY = ENV.get("DEEPSEEK_API_KEY", "")
GITHUB_TOKEN = ENV.get("GITHUB_TOKEN", "")
GITHUB_REPO = "igalvadim-debug/Analitik"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

OUT_DIR = Path("build")
OUT_HTML = OUT_DIR / "index.html"
CACHE = Path("analysis_cache.json")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Du bist ein Immobilien-Analyst fuer Zwangsversteigerungen in Deutschland. "
    "Antworte AUSSCHLIESSLICH mit gueltigem JSON. Kein Markdown, kein ```json, "
    "kein Kommentar, kein Text vor/nach dem JSON. Nur das JSON-Objekt."
)

USER_PROMPT = """Analysiere den Zwangsversteigerungs-Text (Beschluss + Gutachten).
Erstelle exakt dieses JSON mit ALLEN Feldern:

{
  "id": "<file_id>",
  "addr": "<Adresse>",
  "bezirk": "<Stadtteil>",
  "type": "Wohnung|Haus|Gewerbe|Erbbaurecht|Lager|Garage|Stellplatz|Sonstiges",
  "wfl": <Wohnflaeche m² oder null>,
  "rooms": <Zimmer oder null>,
  "bj": <Baujahr oder null>,
  "vw": <Verkehrswert EUR oder null>,
  "date": "<Termin YYYY-MM-DD oder 'unbekannt'>",
  "beschluss": true/false,
  "gutachten": true/false,
  "pros": ["Vorteil 1", "Vorteil 2", "Vorteil 3"],
  "cons": ["Nachteil 1", "Nachteil 2", "Nachteil 3"],
  "risks": "<Hauptrisiken, 1 Satz>",
  "risk": "low|med|high",
  "markt": "<Marktpreis-Spanne, z.B. 350-400 kEUR>",
  "discount": "<Abschlag zum Markt, z.B. ~10%25 unter Markt>",
  "liqui": "hoch|mittel|niedrig|sehr niedrig",
  "miete": "<Mietspanne pro Monat, z.B. 1200-1500 EUR/Mo>",
  "yld": "<Bruttorendite, z.B. ~4.5%25>",
  "costs": "<Sanierungskosten, z.B. 30-60 kEUR>",
  "verdict": "<1-2 Saetze Kaufempfehlung inkl. maximalem Gebot>",
  "bid": "<Maximales Gebot, z.B. 280-300 kEUR>"
}

Regeln:
- ALLE Felder muessen vorhanden sein, auch wenn leer oder null.
- risk: low=kaum Risiken, med=uebliche Risiken, high=erhebliche Risiken.
- type aus Text: Eigentumswohnung=Wohnung, EFH/REH/DHH=Haus, etc.
- Beschluss = Amtliche Bekanntmachung; Gutachten = Verkehrswertgutachten.
- Schaetze konservativ bei fehlenden Daten.

TEXT:
"""


# ---------------------------------------------------------------------------
# JSON extraction + fallback
# ---------------------------------------------------------------------------
def extract_json(raw: str) -> dict | None:
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


def fallback_parse(raw: str, obj_id: str) -> dict:
    def first(pattern, default=None):
        m = re.search(pattern, raw, re.IGNORECASE)
        return m.group(1).strip() if m else default

    addr = (first(r"\*\*Objekt:?\*\*\s*(.+?)(?:\n|$)")
            or first(r"Objekt:?\s*(.+?)(?:\n|$)")
            or first(r"Adresse:?\s*(.+?)(?:\n|$)")
            or f"Objekt {obj_id}")
    bezirk = first(r"(?:Stadtteil|Bezirk|Lage|Ort):?\s*(.+?)(?:\n|$)", "")
    vw_str = first(r"Verkehrswert:?\s*([\d.,]+\s*(?:EUR|€)?)", "0")
    vw = int(re.sub(r"[^\d]", "", vw_str)) if re.sub(r"[^\d]", "", vw_str) else None
    wfl_str = first(r"Wohnfläche:?\s*(\d+)")
    wfl = int(wfl_str) if wfl_str else None
    rooms_str = first(r"Zimmer(?:zahl)?:?\s*(\d+)")
    rooms = int(rooms_str) if rooms_str else None
    bj_str = first(r"Baujahr:?\s*(\d{4})")
    bj = int(bj_str) if bj_str else None
    date = first(r"Termin:?\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})", "unbekannt")

    tl = raw.lower()
    if "eigentumswohnung" in tl or "wohnung" in tl: typ = "Wohnung"
    elif "einfamilienhaus" in tl or "wohnhaus" in tl or "reihenhaus" in tl: typ = "Haus"
    elif "gewerbe" in tl or "laden" in tl: typ = "Gewerbe"
    elif "erbbaurecht" in tl: typ = "Erbbaurecht"
    elif "garage" in tl or "stellplatz" in tl: typ = "Stellplatz"
    else: typ = "Sonstiges"

    risk_signals = ["keine innenbesichtigung", "sanierung", "modernisierung",
                    "bauschaden", "feucht", "schimmel", "asbest",
                    "baulast", "denkmalschutz", "zuwegung"]
    count = sum(1 for s in risk_signals if s in tl)
    risk = "high" if count >= 4 or "kernsanierung" in tl or "abbruch" in tl else ("low" if count <= 1 else "med")

    return {
        "id": obj_id, "addr": addr, "bezirk": bezirk, "type": typ,
        "wfl": wfl, "rooms": rooms, "bj": bj, "vw": vw, "date": date,
        "beschluss": "beschluss" in tl or "bekanntmachung" in tl,
        "gutachten": "gutachten" in tl,
        "photos": [], "zvg_link": "",
        "pros": ["[Fallback-Parse]"], "cons": ["[Fallback-Parse]"],
        "risks": first(r"(?:Risiken|Hauptrisiko):?\s*(.+?)(?:\n|$)", "Siehe Gutachten"),
        "risk": risk, "markt": "?", "discount": "?", "liqui": "mittel",
        "miete": "?", "yld": "?", "costs": "?",
        "verdict": "Automatische Analyse fehlgeschlagen. Manuell pruefen.", "bid": "?"
    }


# ---------------------------------------------------------------------------
# DeepSeek API call
# ---------------------------------------------------------------------------
def analyze_md(md_path: Path) -> dict | None:
    text = md_path.read_text(encoding="utf-8")
    if len(text) > 24000:
        text = text[:18000] + "\n\n[... gekuerzt ...]\n\n" + text[-6000:]

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT + text},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }

    for attempt in range(3):
        try:
            resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=120)
            if resp.status_code != 200:
                print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(3)
                continue
            content = resp.json()["choices"][0]["message"]["content"]
            data = extract_json(content)
            if data:
                return data
            if attempt == 2:
                data = fallback_parse(content, md_path.stem)
                if data:
                    print(f"  -> Fallback-Parse")
                    return data
            print(f"  JSON parse failed, retry {attempt+1}/3")
            print(f"  Raw: {content[:200]}")
        except Exception as e:
            print(f"  Request error: {e}")
        time.sleep(2)
    return None


# ---------------------------------------------------------------------------
# GitHub upload
# ---------------------------------------------------------------------------
def gh_headers(token: str) -> dict:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}


def gh_clear_dir(token: str, repo: str, path_in_repo: str):
    """Loescht alle Dateien in einem GitHub-Verzeichnis."""
    api_url = f"https://api.github.com/repos/{repo}/contents/{path_in_repo}"
    try:
        r = requests.get(api_url, headers=gh_headers(token), timeout=15)
        if r.status_code != 200:
            return
        for item in r.json():
            if item.get("type") != "file":
                continue
            del_url = f"{api_url}/{item['name']}"
            body = {"message": "clear old", "sha": item["sha"], "branch": "main"}
            requests.delete(del_url, json=body, headers=gh_headers(token), timeout=15)
    except Exception as e:
        print(f"  GitHub clear warn: {e}")


def gh_upload_file(token: str, repo: str, path_in_repo: str, file_path: Path):
    """Upload eine einzelne Datei zu GitHub."""
    content_b64 = base64.b64encode(file_path.read_bytes()).decode()
    api_url = f"https://api.github.com/repos/{repo}/contents/{path_in_repo}"
    sha = None
    try:
        r = requests.get(api_url, headers=gh_headers(token), timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    body = {"message": "update", "content": content_b64, "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(api_url, json=body, headers=gh_headers(token), timeout=30)
    return r.status_code in (200, 201)


def upload_all(token: str, repo: str, build_dir: Path):
    """Upload build/index.html + build/pic/ + build/pic/thmb/ nach GitHub."""
    if not token:
        print("WARNUNG: GITHUB_TOKEN nicht gesetzt — kein Upload.")
        return

    obj_count = len(json.loads(CACHE.read_text(encoding="utf-8"))) if CACHE.exists() else 0
    commit_msg = f"Update — {obj_count} Objekte"

    # 1. index.html
    print("  Upload index.html ...")
    ok = gh_upload_file(token, repo, "build/index.html", build_dir / "index.html")
    if ok:
        print(f"    -> OK: https://github.com/{repo}/blob/main/build/index.html")
    else:
        print("    -> FEHLER")

    # 2. Alte Bilder loeschen
    print("  Loesche alte Bilder in build/pic/ ...")
    gh_clear_dir(token, repo, "build/pic")

    # 3. Neue Bilder uploaden
    pic_dir = build_dir / "pic"
    if pic_dir.exists():
        for f in sorted(pic_dir.glob("*")):
            if f.is_file():
                gh_upload_file(token, repo, f"build/pic/{f.name}", f)
        print(f"    -> {len(list(pic_dir.glob('*')))} Bilder in build/pic/")

    thmb_dir = pic_dir / "thmb"
    if thmb_dir.exists():
        for f in sorted(thmb_dir.glob("*")):
            if f.is_file():
                gh_upload_file(token, repo, f"build/pic/thmb/{f.name}", f)
        print(f"    -> {len(list(thmb_dir.glob('*')))} Thumbnails in build/pic/thmb/")


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------
def generate_html(items: list[dict], output_path: Path):
    items_json = json.dumps(items, ensure_ascii=False, indent=2)
    n = len(items)

    html = f'''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zwangsversteigerung — {n} Objekte</title>
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
.card-row{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}}
.pill{{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid #e0dfd8;color:#5f5e5a}}
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
.verdict b{{color:#1a1a18;font-weight:500}}
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
</style>
</head>
<body>
<div class="header">
<h1>Zwangsversteigerung &mdash; {n} Objekte</h1>
<p>Analyse per DeepSeek &middot; {n} Objekte</p>
</div>
<div class="stats-row">
<div class="stat"><div class="stat-label">Objekte gesamt</div><div class="stat-val" id="s-total">{n}</div></div>
<div class="stat"><div class="stat-label">Min. Verkehrswert</div><div class="stat-val" id="s-min">&mdash;</div></div>
<div class="stat"><div class="stat-label">Max. Verkehrswert</div><div class="stat-val" id="s-max">&mdash;</div></div>
<div class="stat"><div class="stat-label">Naechster Termin</div><div class="stat-val" id="s-next">&mdash;</div></div>
</div>
<div class="filters">
<label>Typ:</label>
<select id="f-type" onchange="render()"><option value="">Alle</option><option value="Wohnung">Wohnung</option><option value="Haus">Haus</option><option value="Gewerbe">Gewerbe</option><option value="Erbbaurecht">Erbbaurecht</option><option value="Lager">Lager</option></select>
<label>Risiko:</label>
<select id="f-risk" onchange="render()"><option value="">Alle</option><option value="low">Niedrig</option><option value="med">Mittel</option><option value="high">Hoch</option></select>
<label>Sort:</label>
<select id="f-sort" onchange="render()"><option value="date">Datum</option><option value="asc">Preis &uarr;</option><option value="desc">Preis &darr;</option></select>
<input type="text" id="f-search" placeholder="Suche Adresse, Stadtteil..." oninput="render()" style="min-width:160px">
</div>
<div class="grid" id="grid"></div>
<div class="no-results" id="no-results" style="display:none">Keine Ergebnisse</div>
<script>
const DATA={items_json};
const typeColor={{"Wohnung":"background:#e6f1fb;color:#185fa5","Haus":"background:#eaf3de;color:#3b6d11","Gewerbe":"background:#faeeda;color:#854f0b","Erbbaurecht":"background:#fbeaf0;color:#993556","Lager":"background:#f1efe8;color:#5f5e5a","Garage":"background:#f1efe8;color:#5f5e5a","Stellplatz":"background:#f1efe8;color:#5f5e5a","Sonstiges":"background:#f1efe8;color:#5f5e5a"}};
const riskLabel={{"low":"Niedriges Risiko","med":"Mittleres Risiko","high":"Hohes Risiko"}};
function fmtPrice(v){{if(!v)return"\u2014";if(v>=1e6)return(v/1e6).toFixed(2).replace(/\\.?0+$/,"")+" Mio EUR";return(v/1e3).toFixed(0)+" kEUR"}}
function render(){{
let data=[...DATA];
const tf=document.getElementById("f-type").value,rf=document.getElementById("f-risk").value,sf=document.getElementById("f-sort").value,search=document.getElementById("f-search").value.toLowerCase();
if(tf)data=data.filter(d=>d.type===tf);
if(rf)data=data.filter(d=>d.risk===rf);
if(search)data=data.filter(d=>(d.addr||"").toLowerCase().includes(search)||(d.bezirk||"").toLowerCase().includes(search));
if(sf==="asc")data.sort((a,b)=>(a.vw||0)-(b.vw||0));else if(sf==="desc")data.sort((a,b)=>(b.vw||0)-(a.vw||0));else data.sort((a,b)=>(a.date||"z").localeCompare(b.date||"z"));
document.getElementById("grid").innerHTML=data.map(d=>`<div class="card"><div class="card-head"><span class="card-id">#${{d.id}}</span><span class="card-type" style="${{typeColor[d.type]||typeColor["Sonstiges"]}}">${{d.type||"?"}}</span></div><div class="card-addr">${{d.addr||"Unbekannt"}}</div><div class="card-sub">${{d.bezirk||""}}${{d.wfl?" \u00b7 "+d.wfl+" m\u00b2":""}}${{d.rooms?" \u00b7 "+d.rooms+" Zi":""}}${{d.bj?" \u00b7 BJ "+d.bj:""}}</div>${{(d.photos||[]).length?`<div class="card-photos">${{d.photos.map(p=>`<img src="${{p}}" alt="Foto" loading="lazy">`).join("")}}</div>`:""}}<div class="card-price">${{fmtPrice(d.vw)}} <span class="date-badge">${{d.date||"?"}}</span></div><div class="risk-row"><span class="risk-badge risk-${{d.risk||"med"}}">${{riskLabel[d.risk]||d.risk}}</span><span class="date-badge">${{d.discount||""}}</span></div>${{(d.pros||[]).length||(d.cons||[]).length?`<div class="pros-cons"><div><div class="pc-label">Pro</div><ul class="pc-list">${{(d.pros||[]).map(p=>`<li>+ ${{p}}</li>`).join("")}}</ul></div><div><div class="pc-label">Contra</div><ul class="pc-list">${{(d.cons||[]).map(c=>`<li>\u2212 ${{c}}</li>`).join("")}}</ul></div></div>`:""}}<div class="fin-block"><div class="fin-row"><span class="fin-label">Marktpreis</span><span>${{d.markt||"\u2014"}}</span></div><div class="fin-row"><span class="fin-label">Miete</span><span>${{d.miete||"\u2014"}}</span></div><div class="fin-row"><span class="fin-label">Rendite</span><span>${{d.yld||"\u2014"}}</span></div><div class="fin-row"><span class="fin-label">Liquiditaet</span><span>${{d.liqui||"\u2014"}}</span></div><div class="fin-row"><span class="fin-label">Risiken</span><span style="text-align:right;max-width:180px">${{d.risks||"\u2014"}}</span></div></div><div class="verdict">${{d.verdict||""}}</div><div class="sources"><span class="${{d.beschluss?"src-yes":"src-no"}}">Beschluss ${{d.beschluss?"OK":"\u2014"}}</span><span class="${{d.gutachten?"src-yes":"src-no"}}">Gutachten ${{d.gutachten?"OK":"\u2014"}}</span></div>${{d.zvg_link?`<a href="${{d.zvg_link}}" target="_blank" rel="noopener" class="zvg-link">ZVG-Portal &rarr;</a>`:""}}</div>`).join("");
document.getElementById("no-results").style.display=data.length?"none":"block";
const vws=data.map(d=>d.vw).filter(v=>v);if(vws.length){{document.getElementById("s-min").textContent=fmtPrice(Math.min(...vws));document.getElementById("s-max").textContent=fmtPrice(Math.max(...vws))}}
const dates=data.map(d=>d.date).filter(d=>d&&d!=="unbekannt").sort();if(dates.length)document.getElementById("s-next").textContent=dates[0]
}}
render();
</script>
</body>
</html>'''
    output_path.write_text(html, encoding="utf-8")
    print(f"  -> {output_path.absolute()}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    md_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("zvg_duss_koeln/md")
    do_upload = "--upload" in sys.argv

    if not DEEPSEEK_KEY:
        print("FEHLER: DEEPSEEK_API_KEY nicht in .env gefunden.")
        sys.exit(1)

    if not md_dir.exists():
        print(f"FEHLER: '{md_dir}' nicht gefunden. Erst pdf_to_md.py ausfuehren.")
        sys.exit(1)

    md_files = sorted(md_dir.glob("*.md"), key=lambda p: p.stem)
    print(f"MD-Dateien: {len(md_files)}")

    cache = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"Cache: {len(cache)} Eintraege")

    results = []
    for i, md_path in enumerate(md_files, 1):
        obj_id = md_path.stem
        print(f"\n[{i}/{len(md_files)}] ID={obj_id} ...")

        if obj_id in cache:
            print(f"  -> aus Cache")
            results.append(cache[obj_id])
            continue

        data = analyze_md(md_path)
        if data:
            data["id"] = str(data.get("id", obj_id))
            # zvg_id aus MD extrahieren (wird nicht von DeepSeek geliefert)
            md_text = md_path.read_text(encoding="utf-8")
            m = re.search(r"zvg_id:\s*(\d+)", md_text)
            zvg_id = m.group(1) if m else obj_id
            data["zvg_link"] = f"https://www.zvg-portal.de/index.php?button=showZvg&zvg_id={zvg_id}&land_abk=nw"
            # Fotos — Pfade relativ zu index.html (img*/foto* aus Gutachten/Fotos-PDFs)
            photo_dir = md_dir / "photos"
            photo_files = []
            if photo_dir.exists():
                photo_files = sorted(
                    list(photo_dir.glob(f"{obj_id}_img*.*")) +
                    list(photo_dir.glob(f"{obj_id}_foto*.*"))
                )
            data["photos"] = [p.as_posix() for p in photo_files]
            results.append(data)
            cache[obj_id] = data
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  -> {data.get('addr','?')} | {data.get('type','?')} | {fmt(data.get('vw'))}")
        else:
            print(f"  -> FEHLER")
            dummy = {"id": obj_id, "addr": f"Objekt {obj_id}", "bezirk": "", "type": "Sonstiges",
                     "wfl": None, "rooms": None, "bj": None, "vw": None, "date": "unbekannt",
                     "beschluss": False, "gutachten": False, "photos": [], "zvg_link": "",
                     "pros": [], "cons": [],
                     "risks": "Analyse fehlgeschlagen", "risk": "med", "markt": "?", "discount": "?",
                     "liqui": "?", "miete": "?", "yld": "?", "costs": "?",
                     "verdict": "Keine Analyse moeglich.", "bid": "?"}
            results.append(dummy)
            cache[obj_id] = dummy
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(0.3)

    print(f"\n{'='*60}")
    print(f"Analysiert: {len(results)}")

    # HTML-Daten: Foto-Pfade auf pic/thmb/ umbiegen
    for r in results:
        r["photos"] = ["pic/thmb/" + Path(p).name for p in r.get("photos", [])]

    # build/ vorbereiten
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pic_dir = OUT_DIR / "pic"
    thmb_dir = pic_dir / "thmb"
    pic_dir.mkdir(parents=True, exist_ok=True)
    thmb_dir.mkdir(parents=True, exist_ok=True)

    # Fotos + Thumbnails aus md/photos/ nach build/pic/ kopieren
    src_photos = md_dir / "photos"
    if src_photos.exists():
        for f in src_photos.glob("*"):
            if f.is_file():
                shutil.copy2(f, pic_dir / f.name)
        src_thmb = src_photos / "thmb"
        if src_thmb.exists():
            for f in src_thmb.glob("*"):
                if f.is_file():
                    shutil.copy2(f, thmb_dir / f.name)

    generate_html(results, OUT_HTML)
    print(f"  -> {OUT_HTML.absolute()}")

    if do_upload:
        print("Upload zu GitHub...")
        upload_all(GITHUB_TOKEN, GITHUB_REPO, OUT_DIR)
    else:
        print("Hinweis: --upload fuer GitHub-Upload anhaengen.")

    print("Fertig!")


def fmt(v):
    if v is None: return "?"
    if v >= 1_000_000: return f"{v/1_000_000:.2f} Mio"
    return f"{v/1000:.0f}k"


if __name__ == "__main__":
    main()
