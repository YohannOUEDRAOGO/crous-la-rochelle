"""
Surveillance des logements CROUS à La Rochelle -> notification téléphone (ntfy)

Interroge directement l'API du site officiel trouverunlogement.lescrous.fr
et t'envoie une notification urgente dès qu'un NOUVEAU logement apparaît.

Installation :  pip install requests
Lancement   :  python crous_la_rochelle.py
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

# ============ CONFIGURATION ============

# Nom long et unique : sur ntfy.sh, toute personne qui le connaît peut lire tes notifs.
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "crous-yohann-a-changer-x7k2p9")

# RUN_ONCE=1 : une seule vérification puis arrêt (mode GitHub Actions)
RUN_ONCE = os.getenv("RUN_ONCE") == "1"

# Zone surveillée : La Rochelle + Lagord, Aytré, Puilboreau, Périgny
LON_OUEST, LAT_NORD = -1.26, 46.21
LON_EST, LAT_SUD = -1.04, 46.11

PRIX_MAX_EUROS = None   # ex : 450 ; None = pas de limite
INTERVALLE = 120        # secondes entre deux vérifications

# =======================================

BASE = "https://trouverunlogement.lescrous.fr"
FICHE = BASE + "/tools/{phase}/accommodations/{id}"
ETAT = Path(__file__).with_name("logements_vus.json")
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Origin": BASE,
    "Referer": BASE + "/",
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def notifier(titre, message, lien=None, priorite=5):
    corps = {"topic": NTFY_TOPIC, "title": titre, "message": message,
             "priority": priorite, "tags": ["house", "rotating_light"]}
    if lien:
        corps["click"] = lien
        corps["actions"] = [{"action": "view", "label": "Voir l'annonce", "url": lien}]
    try:
        requests.post("https://ntfy.sh", json=corps, timeout=20)
    except requests.RequestException as e:
        log(f"Erreur ntfy : {e}")


def phases_actives():
    """Liste des phases de réservation ouvertes (l'ID change à chaque phase)."""
    r = requests.get(BASE + "/api/fr/tools", headers=HEADERS, timeout=30)
    r.raise_for_status()
    maintenant = datetime.now().astimezone()
    actives = []
    for t in r.json():
        try:
            debut = datetime.fromisoformat(t["startDate"])
            fin = datetime.fromisoformat(t["endDate"])
        except (KeyError, ValueError):
            continue
        if t.get("enabled") and t.get("published") and debut <= maintenant <= fin:
            actives.append(t)
    return actives


def chercher(phase):
    """Tous les logements disponibles dans la zone pour une phase donnée."""
    items, page = [], 1
    while True:
        payload = {
            "idTool": phase["id"],
            "need_aggregation": True,
            "page": page,
            "pageSize": 200,
            "sector": None,
            "occupationModes": [],
            "location": [{"lon": LON_OUEST, "lat": LAT_NORD},
                         {"lon": LON_EST, "lat": LAT_SUD}],
            "residence": None,
            "precision": 7,
            "equipment": [],
            "price": {"min": 0,
                      "max": PRIX_MAX_EUROS * 100 if PRIX_MAX_EUROS else 10_000_000},
            "toolMechanism": phase.get("mechanism", "residual"),
        }
        r = requests.post(f"{BASE}/api/fr/search/{phase['id']}",
                          json=payload, headers=HEADERS, timeout=30)
        r.raise_for_status()
        res = r.json().get("results") or {}
        lot = res.get("items") or []
        items += lot
        total = res.get("total", 0)
        if isinstance(total, dict):
            total = total.get("value", 0)
        if not lot or len(items) >= int(total) or page >= 20:
            return items
        page += 1


def decrire(item):
    nom = item.get("label") or "Logement"
    res = (item.get("residence") or {}).get("label") or ""
    loyer = ""
    try:
        rent = (item.get("occupationModes") or [{}])[0].get("rent") or {}
        if rent.get("min") is not None:
            loyer = f"{rent['min'] / 100:.0f} €"
            if rent.get("max") and rent["max"] != rent["min"]:
                loyer += f"–{rent['max'] / 100:.0f} €"
    except (AttributeError, TypeError, IndexError):
        pass
    return nom, " · ".join(x for x in (res, loyer) if x)


def charger_etat():
    try:
        return json.loads(ETAT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"vus": [], "phases_init": [], "alerte_aucune_phase": False}


def main():
    etat = charger_etat()
    vus = set(etat["vus"])
    phases_init = set(etat.get("phases_init", []))
    alerte_aucune_phase = etat.get("alerte_aucune_phase", False)
    log("Surveillance CROUS La Rochelle démarrée.")

    while True:
        try:
            phases = phases_actives()
            if not phases:
                log("Aucune phase de réservation ouverte en ce moment.")
                if not alerte_aucune_phase:
                    notifier("CROUS : aucune phase ouverte",
                             "Le script attend l'ouverture d'une nouvelle phase.", priorite=3)
                    alerte_aucune_phase = True
            else:
                alerte_aucune_phase = False

            for phase in phases:
                pid = phase["id"]
                items = chercher(phase)
                cles = {f"{pid}:{i['id']}" for i in items if i.get("id") is not None}

                if pid not in phases_init:
                    # Premier passage sur cette phase : on enregistre l'existant sans spammer
                    vus |= cles
                    phases_init.add(pid)
                    log(f"Phase « {phase['name']} » : {len(cles)} logement(s) déjà en ligne.")
                    notifier("Surveillance CROUS active ✅",
                             f"{phase['name']} : {len(cles)} logement(s) actuellement "
                             "à La Rochelle. Tu seras prévenu des nouveaux.", priorite=3)
                    continue

                for item in items:
                    cle = f"{pid}:{item.get('id')}"
                    if cle in vus:
                        continue
                    vus.add(cle)
                    nom, details = decrire(item)
                    lien = FICHE.format(phase=pid, id=item["id"])
                    log(f"NOUVEAU : {nom} | {details} | {lien}")
                    notifier(f"🏠 Nouveau logement : {nom}",
                             details or "La Rochelle", lien)

                log(f"{len(cles)} logement(s) en ligne, aucun autre nouveau.")

            ETAT.write_text(json.dumps({"vus": sorted(vus),
                                        "phases_init": sorted(phases_init),
                                        "alerte_aucune_phase": alerte_aucune_phase}),
                            encoding="utf-8")
        except requests.RequestException as e:
            log(f"Erreur réseau : {e} (nouvel essai au prochain tour)")
        except Exception as e:
            log(f"Erreur inattendue : {type(e).__name__} : {e}")

        if RUN_ONCE:
            return
        time.sleep(INTERVALLE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Arrêt.")
