#!/usr/bin/env python3
from __future__ import annotations

import asyncio
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
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
RESULTS_PATH = ROOT / "latest-results.json"

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
    price: str | None
    checked_at: str


def normalize(value: str) -> str:
    value = value.lower().replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def extract_price(text: str) -> str | None:
    values: list[float] = []
    for raw in re.findall(r"(\d{2,4}(?:[.,]\d{1,2})?)\s*€", text):
        try:
            amount = float(raw.replace(",", "."))
            if 500 <= amount <= 1800:
                values.append(amount)
        except ValueError:
            pass
    if not values:
        return None
    amount = min(values)
    return (f"{amount:.2f} €").replace(".00 €", " €").replace(".", ",")


def contains_any(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if normalize(term) in text]


def classify(site: dict[str, Any], raw_text: str) -> tuple[str, str]:
    text = normalize(raw_text)
    key = site["key"]

    negatives = contains_any(text, site.get("negative_terms", []))
    positives = contains_any(text, site.get("positive_terms", []))
    checks = contains_any(text, site.get("check_terms", []))

    if key == "manomano":
        if "produit épuisé" in text or "produit epuise" in text:
            return UNAVAILABLE, "La fiche affiche « Produit épuisé »"
        if "ajouter au panier" in text and not negatives:
            return AVAILABLE, "Le produit n’est plus marqué épuisé et le panier est proposé"
        return UNAVAILABLE, "Aucune commande ManoMano détectée"

    if key.startswith("optimea_"):
        if "rupture de stock" in text or "indisponible" in text:
            return UNAVAILABLE, "Optimea affiche une rupture ou une indisponibilité"
        if "ajouter au panier" in text:
            return AVAILABLE, "Optimea affiche un bouton panier"
        if "en stock" in text and "plus que" in text:
            return AVAILABLE, "Optimea affiche une quantité en stock"
        return UNAVAILABLE, "Aucun signal de commande Optimea détecté"

    if key == "amazon":
        if any(term in text for term in [
            "actuellement indisponible",
            "aucune offre en vedette disponible",
            "nous ne savons pas quand cet article sera de nouveau approvisionné",
        ]):
            return UNAVAILABLE, "Amazon n’affiche aucune offre commandable"
        if "ajouter au panier" in text or "acheter maintenant" in text:
            return CHECK, "Une offre Amazon semble commandable : vérifie le vendeur et le prix"
        return UNAVAILABLE, "Aucune offre Amazon commandable détectée"

    if key == "darty":
        if "produit indisponible" in text or "indisponible" in text:
            return UNAVAILABLE, "Darty affiche une indisponibilité"
        if "ajouter au panier" in text or "disponible en livraison" in text:
            return AVAILABLE, "Darty affiche un signal de commande"
        return UNAVAILABLE, "Aucune commande Darty détectée"

    if key == "castorama":
        # Le texte du bouton reste dans la page même quand il est grisé.
        # Les messages suivants signifient que la commande n'est pas ouverte.
        if any(term in text for term in [
            "vérifiez sa disponibilité auprès de votre magasin",
            "verifiez sa disponibilite aupres de votre magasin",
            "ce produit rencontre un grand succès",
            "ce produit rencontre un grand succes",
            "indisponible",
            "rupture de stock",
            "non disponible",
        ]):
            return UNAVAILABLE, "Castorama demande de vérifier le magasin et le bouton panier est désactivé"
        if "ajouter au panier" in text:
            return CHECK, "Panier Castorama potentiellement actif : vérifie livraison et retrait"
        return UNAVAILABLE, "Aucune commande Castorama détectée"

    if key == "leroymerlin":
        if negatives:
            return UNAVAILABLE, f"Signal négatif : {negatives[0]}"
        if "ajouter au panier" in text or "livraison à domicile" in text:
            return CHECK, "Commande ou livraison potentielle détectée : vérifie ton code postal"
        if "sur commande aujourd'hui" in text:
            return CHECK, "Mention « Sur commande aujourd’hui » détectée"
        return UNAVAILABLE, "Aucune commande Leroy Merlin détectée"

    if key == "boulanger":
        if negatives:
            return UNAVAILABLE, f"Signal négatif : {negatives[0]}"
        if "ajouter au panier" in text:
            return AVAILABLE, "Bouton « Ajouter au panier » détecté"
        if "retrait magasin disponible" in text or "livraison à domicile" in text:
            return CHECK, "Disponibilité potentielle détectée"
        return UNAVAILABLE, "Aucune commande Boulanger détectée"

    if key == "bricorama":
        # Ne jamais se baser sur le mot "portasplit" seul :
        # il apparaît forcément dans le champ de recherche et le titre de la page.
        if any(term in text for term in [
            "nous n'avons pas trouvé de résultat",
            "nous n’avons pas trouvé de résultat",
            "aucun résultat",
            "aucun produit",
        ]):
            return UNAVAILABLE, "Bricorama ne trouve aucun produit correspondant"

        # Exige des indices d'une vraie carte produit.
        has_model = "mmcs-12hrn8-qrd0" in text or "climatiseur portasplit midea" in text
        has_commercial_signal = any(term in text for term in [
            "ajouter au panier",
            "vendu et expédié par",
            "vendu et expedie par",
            "retrait magasin",
            "livraison",
        ])
        if has_model and has_commercial_signal:
            return CHECK, "Une vraie fiche produit PortaSplit semble apparaître chez Bricorama"
        return UNAVAILABLE, "Aucune vraie fiche produit PortaSplit détectée chez Bricorama"

    if negatives:
        return UNAVAILABLE, f"Signal négatif : {negatives[0]}"
    if positives:
        return AVAILABLE, f"Signal positif : {positives[0]}"
    if checks:
        return CHECK, f"Changement à vérifier : {checks[0]}"
    return UNAVAILABLE, "Aucun signal de commande détecté"


async def dismiss_cookies(page) -> None:
    labels = [
        r"Tout accepter",
        r"Accepter tout",
        r"J.?accepte",
        r"Accepter",
        r"Continuer sans accepter",
        r"Autoriser tous les cookies",
    ]
    for label in labels:
        try:
            locator = page.get_by_role("button", name=re.compile(label, re.I))
            if await locator.count():
                await locator.first.click(timeout=1200)
                await page.wait_for_timeout(500)
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
        await page.wait_for_timeout(700)
        text = await page.locator("body").inner_text(timeout=20_000)
        status, reason = classify(site, text)
        return Result(
            key=site["key"],
            name=site["name"],
            url=site["url"],
            status=status,
            reason=reason,
            price=extract_price(text),
            checked_at=now,
        )
    except PlaywrightTimeoutError:
        return Result(
            site["key"], site["name"], site["url"], ERROR,
            "Le chargement a dépassé le délai", None, now
        )
    except Exception as exc:
        return Result(
            site["key"], site["name"], site["url"], ERROR,
            f"{type(exc).__name__}: {exc}", None, now
        )
    finally:
        await page.close()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def post_ntfy(topic: str, result: Result) -> None:
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    title = f"PortaSplit : {result.status} chez {result.name}"
    price = f"\nPrix détecté : {result.price}" if result.price else ""
    body = (
        f"{result.reason}{price}\n"
        "Ouvre la fiche et vérifie immédiatement la livraison ou le retrait à Paris."
    )
    headers = {
        "Title": title,
        "Priority": "urgent",
        "Tags": "rotating_light,snowflake",
        "Click": result.url,
    }
    request = urllib.request.Request(
        f"{server}/{urllib.parse.quote(topic)}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


async def main() -> None:
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    topic = os.getenv("NTFY_TOPIC", "").strip()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        results: list[Result] = []
        for site in config["sites"]:
            if site.get("enabled", True):
                result = await inspect(context, site)
                results.append(result)
                suffix = f" — {result.price}" if result.price else ""
                print(f"{result.name}: {result.status}{suffix} — {result.reason}")
        await browser.close()

    for result in results:
        previous = state.get(result.key, {})
        previous_status = previous.get("status")

        should_alert = (
            result.status in {AVAILABLE, CHECK}
            and result.status != previous_status
        )

        if should_alert:
            if topic:
                try:
                    post_ntfy(topic, result)
                    print(f"Notification envoyée pour {result.name}")
                except Exception as exc:
                    print(f"Échec notification {result.name}: {exc}")
            else:
                print("NTFY_TOPIC absent : aucune notification envoyée")

        if result.status != ERROR:
            state[result.key] = asdict(result)
        elif result.key not in state:
            state[result.key] = asdict(result)

    save_json(STATE_PATH, state)
    save_json(RESULTS_PATH, [asdict(r) for r in results])


if __name__ == "__main__":
    asyncio.run(main())
