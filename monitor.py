#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "state.json"
RESULTS_PATH = ROOT / "latest-results.json"
DASHBOARD_PATH = ROOT / "docs" / "index.html"

AVAILABLE = "DISPONIBLE"
CHECK = "À VÉRIFIER"
UNAVAILABLE = "INDISPONIBLE"
ERROR = "ERREUR"

@dataclass
class Result:
    key: str
    name: str
    url: str
    status: str
    reason: str
    price: float | None
    seller: str | None
    checked_at: str


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("\xa0", " ")).strip()


def prices(text: str) -> list[float]:
    found: list[float] = []
    for raw in re.findall(r"(?<!\d)(\d{2,4}(?:[.,]\d{1,2})?)\s*€", text):
        try:
            value = float(raw.replace(",", "."))
            if 500 <= value <= 2000:
                found.append(value)
        except ValueError:
            pass
    return sorted(set(found))


def first_price(text: str) -> float | None:
    values = prices(text)
    return values[0] if values else None


def classify(site: dict[str, Any], raw: str) -> tuple[str, str, float | None, str | None]:
    text = norm(raw)
    price = first_price(raw)
    max_price = float(CONFIG.get("max_price_eur", 1050))
    key = site["key"]
    seller = None

    if key == "manomano":
        if "produit épuisé" in text or "produit epuise" in text:
            return UNAVAILABLE, "La fiche affiche Produit épuisé", price, None
        if "ajouter au panier" in text and price and price <= max_price:
            return AVAILABLE, "Panier détecté à un prix acceptable", price, None
        if "ajouter au panier" in text:
            return CHECK, "Panier détecté, mais prix à vérifier", price, None
        return UNAVAILABLE, "Aucune offre commandable détectée", price, None

    if key.startswith("optimea_") and key != "optimea_category":
        if "rupture de stock" in text or "indisponible" in text:
            return UNAVAILABLE, "Optimea affiche une rupture de stock", price, "Optimea"
        if "ajouter au panier" in text and (price is None or price <= max_price):
            return AVAILABLE, "Bouton Ajouter au panier détecté", price, "Optimea"
        return CHECK, "La fiche Optimea a changé, vérification manuelle conseillée", price, "Optimea"

    if key == "optimea_category":
        # Cette page sert de filet de sécurité si une nouvelle fiche apparaît.
        if "ajouter au panier" in text and "portasplit" in text:
            return CHECK, "La catégorie PortaSplit contient un bouton panier", price, "Optimea"
        return UNAVAILABLE, "Aucun panier PortaSplit détecté dans la catégorie", price, "Optimea"

    if key == "boulanger":
        negatives = ["produit indisponible", "retrait indisponible", "livraison indisponible"]
        if any(x in text for x in negatives):
            return UNAVAILABLE, "Boulanger indique une indisponibilité", price, "Boulanger"
        if "ajouter au panier" in text and (price is None or price <= max_price):
            return AVAILABLE, "Bouton Ajouter au panier détecté", price, "Boulanger"
        if "retrait magasin" in text or "livraison à domicile" in text:
            return CHECK, "Un mode d’obtention est apparu", price, "Boulanger"
        return UNAVAILABLE, "Aucun signal de commande détecté", price, "Boulanger"

    if key == "leroymerlin":
        if "ce produit n'est plus vendu" in text or "rupture de stock" in text:
            return UNAVAILABLE, "Leroy Merlin indique une indisponibilité", price, "Leroy Merlin"
        if "ajouter au panier" in text:
            return CHECK, "Panier détecté : vérifier le code postal 75000", price, "Leroy Merlin"
        if "sur commande aujourd'hui" in text:
            return CHECK, "Mention Sur commande aujourd’hui détectée", price, "Leroy Merlin"
        return UNAVAILABLE, "Aucun bouton de commande détecté", price, "Leroy Merlin"

    if key == "castorama":
        if any(x in text for x in ["rupture de stock", "non disponible", "indisponible"]):
            return UNAVAILABLE, "Castorama indique une indisponibilité", price, "Castorama"
        if "ajouter au panier" in text:
            return CHECK, "Panier détecté : vérifier magasin et livraison Paris", price, "Castorama"
        return UNAVAILABLE, "Aucun panier détecté", price, "Castorama"

    if key == "amazon":
        if any(x in text for x in [
            "actuellement indisponible",
            "aucune offre en vedette disponible",
            "nous ne savons pas quand cet article sera de nouveau approvisionné",
        ]):
            return UNAVAILABLE, "Amazon n’affiche aucune offre commandable", price, None
        seller_match = re.search(r"vendu par\s+([^\n]+)", raw, re.I)
        seller = seller_match.group(1).strip()[:80] if seller_match else None
        if "ajouter au panier" in text or "acheter maintenant" in text:
            if price and price > max_price:
                return CHECK, f"Offre détectée mais prix supérieur à {max_price:.0f} €", price, seller
            return CHECK, "Offre Amazon détectée : vérifier vendeur et livraison", price, seller
        return UNAVAILABLE, "Aucune offre Amazon commandable détectée", price, seller

    if key == "darty":
        if "produit indisponible" in text or "livraison indisponible" in text:
            return UNAVAILABLE, "Darty indique une indisponibilité", price, "Darty"
        if "ajouter au panier" in text or "disponible en livraison" in text:
            return AVAILABLE, "Signal de commande Darty détecté", price, "Darty"
        return UNAVAILABLE, "Aucun signal de commande détecté", price, "Darty"

    if key == "bricorama":
        product = "portasplit" in text or "mmcs-12hrn8-qrd0" in text
        if product and ("ajouter au panier" in text or "disponible" in text):
            return CHECK, "Une offre PortaSplit semble apparaître", price, "Bricorama"
        if product:
            return CHECK, "Une fiche PortaSplit est apparue dans la recherche", price, "Bricorama"
        return UNAVAILABLE, "Aucun PortaSplit dans les résultats", price, "Bricorama"

    return ERROR, "Règle inconnue", price, seller


async def dismiss_cookies(page) -> None:
    for label in ["Tout accepter", "Accepter tout", "J’accepte", "J'accepte", "Accepter", "Autoriser tous les cookies"]:
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I))
            if await button.count():
                await button.first.click(timeout=1200)
                await page.wait_for_timeout(400)
                return
        except Exception:
            pass


async def inspect(context, site: dict[str, Any]) -> Result:
    page = await context.new_page()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        await page.goto(site["url"], wait_until="domcontentloaded", timeout=50_000)
        await page.wait_for_timeout(site.get("wait_ms", 4500))
        await dismiss_cookies(page)
        await page.wait_for_timeout(500)
        raw = await page.locator("body").inner_text(timeout=20_000)
        status, reason, price, seller = classify(site, raw)
        return Result(site["key"], site["name"], site["url"], status, reason, price, seller, now)
    except PlaywrightTimeoutError:
        return Result(site["key"], site["name"], site["url"], ERROR, "Délai de chargement dépassé", None, None, now)
    except Exception as exc:
        return Result(site["key"], site["name"], site["url"], ERROR, f"{type(exc).__name__}: {exc}", None, None, now)
    finally:
        await page.close()


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def notify(result: Result) -> None:
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC absent : notification ignorée")
        return
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    price = f"\nPrix détecté : {result.price:.2f} €" if result.price else ""
    seller = f"\nVendeur détecté : {result.seller}" if result.seller else ""
    body = f"{result.reason}{price}{seller}\nOuvre la fiche immédiatement et vérifie la livraison à Paris."
    request = urllib.request.Request(
        f"{server}/{urllib.parse.quote(topic)}",
        data=body.encode("utf-8"),
        headers={
            "Title": f"PortaSplit : {result.status} chez {result.name}",
            "Priority": "urgent",
            "Tags": "rotating_light,snowflake",
            "Click": result.url,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def make_dashboard(results: list[Result]) -> None:
    status_class = {AVAILABLE: "ok", CHECK: "warn", UNAVAILABLE: "no", ERROR: "err"}
    cards = []
    for r in results:
        price = f"{r.price:.2f} €" if r.price else "—"
        seller = html.escape(r.seller or "—")
        cards.append(f'''<article class="card {status_class[r.status]}">
<h2>{html.escape(r.name)}</h2><div class="status">{html.escape(r.status)}</div>
<p>{html.escape(r.reason)}</p><dl><dt>Prix</dt><dd>{price}</dd><dt>Vendeur</dt><dd>{seller}</dd></dl>
<a href="{html.escape(r.url)}" target="_blank" rel="noopener">Ouvrir la fiche</a>
</article>''')
    updated = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_PATH.write_text(f'''<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock PortaSplit</title><style>
body{{font-family:system-ui;margin:0;background:#f5f6f8;color:#111}}header{{padding:24px;max-width:1100px;margin:auto}}main{{max-width:1100px;margin:auto;padding:0 24px 40px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}.card{{background:white;border-radius:16px;padding:18px;border-left:8px solid #999;box-shadow:0 2px 10px #0001}}.ok{{border-color:#19a15f}}.warn{{border-color:#e39a18}}.no{{border-color:#b6bcc5}}.err{{border-color:#d33}}.status{{font-weight:800;margin:8px 0}}dl{{display:grid;grid-template-columns:auto 1fr;gap:6px 12px}}dt{{font-weight:700}}a{{display:inline-block;margin-top:12px;padding:10px 14px;border-radius:10px;background:#111;color:#fff;text-decoration:none}}</style>
<header><h1>Surveillance Midea PortaSplit</h1><p>Dernière mise à jour : {updated}. Une mention « À vérifier » demande une validation manuelle du code postal, du vendeur ou du prix.</p></header><main>{''.join(cards)}</main></html>''', encoding="utf-8")


async def main() -> None:
    state = load_state()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris", viewport={"width": 1440, "height": 1000}, user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36")
        results = []
        for site in CONFIG["sites"]:
            result = await inspect(context, site)
            results.append(result)
            print(f"{result.name}: {result.status} — {result.reason}")
        await browser.close()

    for r in results:
        previous = state.get(r.key, {})
        previous_status = previous.get("status")
        if r.status in {AVAILABLE, CHECK} and r.status != previous_status:
            try:
                notify(r)
                print(f"Notification envoyée : {r.name}")
            except Exception as exc:
                print(f"Échec notification {r.name}: {exc}")
        if r.status != ERROR or r.key not in state:
            state[r.key] = asdict(r)

    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    RESULTS_PATH.write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8")
    make_dashboard(results)

if __name__ == "__main__":
    asyncio.run(main())
