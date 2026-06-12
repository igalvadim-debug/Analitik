#!/usr/bin/env python3
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

OUT_DIR = Path("zvg_duss_koeln")
BASE = "https://www.zvg-portal.de/index.php"


def safe_name(s: str) -> str:
    s = s.replace("\xa0", " ").replace("&nbsp;", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return re.sub(r'[<>:"/\\\\|?*]+', "_", s).strip(" ._")


def get_qs_value(url: str, key: str, default: str = "") -> str:
    try:
        return parse_qs(urlparse(url).query).get(key, [default])[0]
    except Exception:
        return default


def normalize_gericht(name: str) -> str:
    """Ersetzt Umlaut-ASCII-Ersatz -> echte Umlaute fuer Matching."""
    replacements = {
        "ae": "ä", "oe": "ö", "ue": "ü",
        "Ae": "Ä", "Oe": "Ö", "Ue": "Ü",
        "ss": "ß",
    }
    for ascii_val, umlaut in replacements.items():
        name = name.replace(ascii_val, umlaut)
    return name


async def open_search(page, gericht: str):
    gericht = normalize_gericht(gericht)
    print(f"\n[{gericht}] Поиск...")
    await page.goto(f"{BASE}?button=Termine%20suchen")
    await page.wait_for_load_state("networkidle")

    await page.evaluate("""
        const sel = document.querySelector('select[name="land_abk"]');
        sel.value = 'nw';
        sel.dispatchEvent(new Event('change', { bubbles: true }));
    """)
    await page.wait_for_timeout(2500)

    ger_options = await page.evaluate("""
        [...document.querySelector('select[name="ger_id"]').options].map(o => ({
            value: o.value,
            text: (o.textContent || '').trim()
        }))
    """)

    ger_id = None
    for opt in ger_options:
        opt_text = normalize_gericht(opt["text"].lower())
        if gericht.lower() in opt_text:
            ger_id = opt["value"]
            break

    if not ger_id:
        raise RuntimeError(f"Не найден ger_id для {gericht}")

    print(f"  ger_id={ger_id}")

    await page.evaluate("""
        (value) => {
            const sel = document.querySelector('select[name="ger_id"]');
            sel.value = value;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
    """, ger_id)

    await asyncio.gather(
        page.wait_for_load_state("networkidle"),
        page.click("input[value='Suchen'], input[name='Submit'], button:text('Suchen')")
    )
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


def extract_pdf_items(links_js: list[dict], gericht: str, seen: set) -> list[dict]:
    """Парсит ссылки из JS-массива [{text, href}] в список словарей."""
    items = []
    for item in links_js:
        href = item["href"]
        text = item["text"] or "pdf"
        file_id = get_qs_value(href, "file_id")
        zvg_id = get_qs_value(href, "zvg_id")
        if not file_id:
            continue
        key = (file_id, zvg_id)
        if key in seen:
            continue
        seen.add(key)
        label_text = text.lower()
        label = (
            "gutachten" if "gutachten" in label_text else
            "bekanntmachung" if "bekanntmachung" in label_text else
            "expose" if "expos" in label_text else
            "anhang"
        )
        items.append({
            "gericht": gericht,
            "file_id": file_id,
            "zvg_id": zvg_id,
            "label": label,
            "text": text,
            "url": href,
        })
    return items


async def collect_pdf_links(page, gericht: str) -> list[dict]:
    """Собирает PDF-ссылки с результатов поиска + с детальных страниц каждого объекта."""
    await open_search(page, gericht)

    # --- 1. Ссылки прямо на странице результатов ---
    search_links = await page.locator("a[href*='button=showAnhang']").evaluate_all("""
        els => els.map(a => ({
            text: ((a.textContent || '').replace(/\\s+/g, ' ')).trim(),
            href: a.href || ''
        }))
    """)

    seen = set()
    all_items = extract_pdf_items(search_links, gericht, seen)
    print(f"  PDF с поиска: {len(all_items)}")

    # --- 2. Собираем уникальные zvg_id с результатов поиска ---
    detail_links = await page.locator("a[href*='button=showZvg']").evaluate_all("""
        els => els.map(a => ({
            text: ((a.textContent || '').replace(/\\s+/g, ' ')).trim(),
            href: a.href || ''
        }))
    """)

    zvg_ids_from_search = set()
    for d in detail_links:
        zvg_id = get_qs_value(d["href"], "zvg_id")
        if zvg_id:
            zvg_ids_from_search.add(zvg_id)

    # Также добавляем zvg_id из уже найденных PDF-ссылок
    for item in all_items:
        if item["zvg_id"]:
            zvg_ids_from_search.add(item["zvg_id"])

    print(f"  Уникальных zvg_id: {len(zvg_ids_from_search)}")

    # --- 3. Заходим на детальную страницу каждого объекта ---
    # Сохраняем URL результатов поиска как referer
    search_url = page.url

    # Собираем точные URL детальных страниц из ссылок на странице поиска
    detail_urls = []
    for d in detail_links:
        zvg_id = get_qs_value(d["href"], "zvg_id")
        if zvg_id and zvg_id in zvg_ids_from_search:
            detail_urls.append((zvg_id, d["href"]))
            zvg_ids_from_search.discard(zvg_id)
    for zvg_id in sorted(zvg_ids_from_search):
        detail_urls.append((zvg_id, f"{BASE}?button=showZvg&land_abk=nw&zvg_id={zvg_id}"))

    detail_new = 0
    first_detail = True

    for zvg_id, detail_url in detail_urls:
        try:
            await page.goto(detail_url, timeout=15000, referer=search_url)
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(800)

            # ДИАГНОСТИКА: для первого объекта
            if first_detail:
                first_detail = False
                html = await page.content()
                (OUT_DIR / "debug_detail.html").write_text(html, encoding="utf-8")
                # Считаем ссылки через JS напрямую
                js_debug = await page.evaluate("""() => {
                    const all = document.querySelectorAll('a');
                    const showAnhang = document.querySelectorAll('a[href*=\"button=showAnhang\"]');
                    return {
                        total_links: all.length,
                        showAnhang_links: showAnhang.length,
                        samples: [...showAnhang].slice(0, 5).map(a => ({
                            text: (a.textContent || '').trim().slice(0, 80),
                            href: a.href.slice(0, 250),
                            attr: a.getAttribute('href') || ''
                        }))
                    };
                }""")
                print(f"    DEBUG: total_links={js_debug['total_links']}, showAnhang={js_debug['showAnhang_links']}")
                print(f"    DEBUG: samples={json.dumps(js_debug['samples'], ensure_ascii=False)}")
                (OUT_DIR / "debug_detail_links.json").write_text(
                    json.dumps(js_debug, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            detail_pdf_links = await page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*=\"button=showAnhang\"]');
                return [...links].map(a => ({
                    text: (a.textContent || '').trim().replace(/\\s+/g, ' '),
                    href: a.href || ''
                }));
            }""")

            before = len(all_items)
            all_items.extend(extract_pdf_items(detail_pdf_links, gericht, seen))
            added = len(all_items) - before
            if added:
                detail_new += added
                labels = [it["label"] for it in all_items[-added:]]
                print(f"  zvg_id={zvg_id} +{added} {labels}")
        except Exception as e:
            print(f"  zvg_id={zvg_id} ОШИБКА: {e}")

    print(f"  PDF с детальных страниц добавлено: {detail_new}")
    print(f"  Всего PDF ссылок: {len(all_items)}")
    return all_items


async def download_pdf(context, item: dict, index: int) -> dict:
    gericht = item["gericht"]
    zvg_id = item["zvg_id"] or "unknown_zvg"
    file_id = item["file_id"]
    label = item["label"]

    folder = OUT_DIR / gericht / f"zvg_{zvg_id}"
    folder.mkdir(parents=True, exist_ok=True)

    filename = f"{index:03d}_{label}_file_{file_id}.pdf"
    path = folder / safe_name(filename)

    meta_path = folder / f"{index:03d}_meta.json"

    result = {
        "status": "ok",
        **item,
        "path": str(path)
    }

    try:
        page_url = f"{BASE}?button=Termine%20suchen"
        resp = await context.request.get(
            item["url"],
            headers={
                "Referer": page_url,
                "Accept": "application/pdf,text/html,*/*",
            },
        )
        body = await resp.body()
        status = resp.status
        content_type = resp.headers.get("content-type", "")

        if body[:4] == b"%PDF":
            path.write_bytes(body)
            print(f"  ✓ zvg_id={zvg_id:<8} file_id={file_id:<8} {len(body)//1024:>5} KB  [{status}]")
        else:
            preview = body[:200].decode("utf-8", errors="replace").replace("\n", " ")
            result["status"] = "not_pdf"
            result["preview"] = preview
            result["http_status"] = status
            result["content_type"] = content_type
            print(f"  ✗ zvg_id={zvg_id:<8} file_id={file_id:<8} HTTP={status} type={content_type[:50]} body={preview[:80]}")
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        print(f"  ✗ zvg_id={zvg_id:<8} file_id={file_id:<8} exc: {e}")

    meta_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    await asyncio.sleep(0.2)
    return result


async def main(gerichte: list[str]):
    if OUT_DIR.exists():
        deleted = 0
        for f in OUT_DIR.rglob("*.json"):
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
        print(f"Удалено старых json: {deleted}")

    OUT_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        all_items = []
        for gericht in gerichte:
            items = await collect_pdf_links(page, gericht)
            all_items.extend(items)

        print(f"\nВсего PDF ссылок: {len(all_items)}")

        results = []
        for i, item in enumerate(all_items, 1):
            print(f"\n[{i}/{len(all_items)}] {item['gericht']} | zvg_id={item['zvg_id']} | file_id={item['file_id']} | {item['text']}")
            results.append(await download_pdf(context, item, i))

        await browser.close()

    ok = sum(1 for r in results if r["status"] == "ok")
    not_pdf = sum(1 for r in results if r["status"] == "not_pdf")
    err = sum(1 for r in results if r["status"] == "error")

    print(f"\n✓ Готово: скачано={ok}, not_pdf={not_pdf}, ошибки={err}")
    print(f"  Папка: {OUT_DIR.absolute()}")


if __name__ == "__main__":
    gerichte = sys.argv[1:] if len(sys.argv) > 1 else ["Düsseldorf", "Köln"]
    print(f"Суди: {', '.join(gerichte)}")
    asyncio.run(main(gerichte))