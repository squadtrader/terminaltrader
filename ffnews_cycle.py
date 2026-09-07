# -*- coding: utf-8 -*-
"""
Script d'extraction quotidienne d'articles - investingLive Central Banks
--------------------------------------------------------------------------
Ce script :
1. Va chercher la liste des articles sur la page Central Banks
   (avec pagination automatique si besoin)
2. Ne garde que les articles publiés dans une fenêtre de temps donnée :
   - Au tout premier lancement (quand deja_vus.txt n'existe pas encore) :
     fenêtre de FENETRE_HEURES_PREMIERE_EXECUTION (15 jours par défaut)
   - Aux lancements suivants : fenêtre de FENETRE_HEURES (24h par défaut)
3. Visite chaque article retenu et en extrait le titre, la date, le contenu
4. Sauvegarde tout dans un fichier nommé avec la date du jour,
   exemple : 02-09-2026-CB.txt
5. Met à jour un fichier articles.json (utilisé par le site index.html)
6. Garde en mémoire les articles déjà récupérés (dans deja_vus.txt)
   pour ne jamais les recopier deux fois.

Deux modes d'exécution :
- Mode "boucle" (par défaut si lancé sans argument) : tourne en continu,
  répète le cycle toutes les INTERVALLE_SECONDES. Pratique en local.
- Mode "une seule fois" (`python ffnews_cycle.py --once`) : fait un seul
  cycle puis s'arrête. C'est ce mode qui est utilisé par le workflow
  GitHub Actions, qui se charge lui-même de la planification (cron).
"""

import re
import sys
import json
import requests
from bs4 import BeautifulSoup
import os
import time
from datetime import datetime, timedelta, timezone
from deep_translator import GoogleTranslator

# ---------- CONFIGURATION ----------
URL_LISTE = "https://investinglive.com/CentralBanks/"
DOSSIER_SORTIE = os.path.dirname(os.path.abspath(__file__))  # dossier où se trouve ce script
FICHIER_DEJA_VUS = os.path.join(DOSSIER_SORTIE, "deja_vus.txt")
FICHIER_JSON = os.path.join(DOSSIER_SORTIE, "articles.json")
MAX_ARTICLES_JSON = 300  # nombre max d'articles conservés dans articles.json (pour ne pas grossir indéfiniment)

# --- Fenêtre de temps ---
FENETRE_HEURES = 24  # fenêtre "normale" (tous les lancements après le premier)
FENETRE_HEURES_PREMIERE_EXECUTION = 15 * 24  # fenêtre pour le tout premier lancement : 15 jours

# --- Pagination ---
# Nombre max de pages à parcourir. Le script s'arrête plus tôt de lui-même
# dès qu'une page ne contient plus que des articles hors fenêtre.
MAX_PAGES_NORMAL = 3
MAX_PAGES_PREMIERE_EXECUTION = 30  # garde-fou large pour couvrir 15 jours

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
GENERER_VERSION_FR = True  # si True, cree en plus un fichier -CB-FR.txt traduit + version FR dans le JSON
LIMITE_CARACTERES_TRADUCTION = 4500  # Google Translate refuse les blocs trop longs (~5000 max)
INTERVALLE_SECONDES = 60  # pause entre deux cycles de verification (mode boucle uniquement)

# Motif des dates affichées sur la page de liste, ex: "04/09/2026 | 16:48 GMT"
MOTIF_DATE_LISTE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*\|\s*(\d{2}:\d{2})\s*GMT")


# ---------- OUTILS ----------

def premiere_execution():
    """Vrai si le fichier deja_vus.txt n'existe pas encore, ce qui indique
    qu'aucun cycle n'a encore tourné sur cette machine/dépôt."""
    return not os.path.exists(FICHIER_DEJA_VUS)


def charger_deja_vus():
    if not os.path.exists(FICHIER_DEJA_VUS):
        return set()
    with open(FICHIER_DEJA_VUS, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def sauvegarder_deja_vu(url):
    with open(FICHIER_DEJA_VUS, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def url_page(numero_page):
    """Construit l'URL d'une page de la liste des articles.
    Page 1 = URL_LISTE elle-même, page 2+ = URL_LISTE + page/N/"""
    if numero_page <= 1:
        return URL_LISTE
    return URL_LISTE.rstrip("/") + f"/page/{numero_page}/"


def extraire_dates_page(texte_html):
    """Extrait toutes les dates de publication affichées sur une page de
    liste (format DD/MM/YYYY | HH:MM GMT) et les renvoie triées."""
    dates = []
    for jour_mois_annee, heure_minute in MOTIF_DATE_LISTE.findall(texte_html):
        try:
            dt = datetime.strptime(
                f"{jour_mois_annee} {heure_minute}", "%d/%m/%Y %H:%M"
            ).replace(tzinfo=timezone.utc)
            dates.append(dt)
        except ValueError:
            continue
    return dates


def recuperer_liens_articles(fenetre_heures, max_pages):
    """Parcourt la page Central Banks (et ses pages suivantes si besoin)
    pour récupérer les liens d'articles. S'arrête dès qu'une page ne
    contient plus que des articles antérieurs à la fenêtre demandée,
    ou après max_pages pages (garde-fou)."""
    maintenant = datetime.now(timezone.utc)
    debut_fenetre = maintenant - timedelta(hours=fenetre_heures)

    liens = set()

    for numero_page in range(1, max_pages + 1):
        url_courante = url_page(numero_page)
        try:
            reponse = requests.get(url_courante, headers=HEADERS, timeout=15)
            reponse.raise_for_status()
        except Exception as e:
            print(f"  -> Impossible de charger la page {numero_page} ({url_courante}) : {e}")
            break

        soup = BeautifulSoup(reponse.text, "html.parser")

        liens_page = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/central-banks/" in href.lower() and href.rstrip("/").lower() != "https://investinglive.com/central-banks":
                if href.startswith("/"):
                    href = "https://investinglive.com" + href
                if href.startswith("https://investinglive.com/central-banks/"):
                    liens_page.add(href.split("?")[0])

        if not liens_page:
            # Page vide ou plus de contenu : on arrête la pagination
            break

        liens |= liens_page
        print(f"  -> Page {numero_page} : {len(liens_page)} lien(s) trouvé(s) (total {len(liens)})")

        # On regarde les dates affichées sur cette page pour savoir si on
        # doit continuer à paginer.
        dates_page = extraire_dates_page(reponse.text)
        if dates_page and min(dates_page) < debut_fenetre:
            print(f"  -> Dates plus anciennes que la fenêtre détectées sur la page {numero_page}, arrêt de la pagination.")
            break

        if numero_page < max_pages:
            time.sleep(1)  # pause polie entre deux pages

    return sorted(liens)


def extraire_meilleur_bloc_de_texte(soup):
    meilleur_conteneur = None
    meilleur_score = 0
    for conteneur in soup.find_all(["div", "article", "section"]):
        paragraphes = conteneur.find_all("p", recursive=False)
        texte = " ".join(p.get_text(strip=True) for p in paragraphes)
        score = len(texte)
        if score > meilleur_score:
            meilleur_score = score
            meilleur_conteneur = conteneur
    if meilleur_conteneur is None:
        return ""
    paragraphes = meilleur_conteneur.find_all("p", recursive=False)
    return "\n\n".join(p.get_text(strip=True) for p in paragraphes if p.get_text(strip=True))


def extraire_date_publication(soup):
    """Cherche la date de publication dans les métadonnées de la page."""
    balise = soup.find("meta", {"property": "article:published_time"})
    if balise and balise.get("content"):
        try:
            # Format ISO renvoyé par le site, ex: 2026-09-02T02:15:37.55Z
            texte_date = balise["content"].replace("Z", "+00:00")
            return datetime.fromisoformat(texte_date)
        except ValueError:
            return None
    return None


def extraire_article(url):
    reponse = requests.get(url, headers=HEADERS, timeout=15)
    reponse.raise_for_status()
    soup = BeautifulSoup(reponse.text, "html.parser")

    titre_tag = soup.find("h1")
    titre = titre_tag.get_text(strip=True) if titre_tag else "Sans titre"

    date_pub = extraire_date_publication(soup)
    contenu = extraire_meilleur_bloc_de_texte(soup)

    return titre, date_pub, contenu


def decouper_texte(texte, limite=LIMITE_CARACTERES_TRADUCTION):
    """Decoupe un texte en morceaux de taille <= limite, en coupant sur des
    paragraphes/phrases plutot qu'au milieu d'un mot, pour respecter les
    limites de l'API de traduction."""
    morceaux = []
    reste = texte
    while len(reste) > limite:
        # on cherche le meilleur point de coupure avant la limite
        coupe = reste.rfind("\n\n", 0, limite)
        if coupe == -1:
            coupe = reste.rfind(". ", 0, limite)
        if coupe == -1:
            coupe = limite
        morceaux.append(reste[:coupe].strip())
        reste = reste[coupe:].strip()
    if reste:
        morceaux.append(reste)
    return morceaux


def traduire_texte(texte, langue_dest="fr"):
    """Traduit un texte en francais. Renvoie le texte original si la
    traduction echoue (ex: probleme reseau)."""
    if not texte:
        return texte
    try:
        traducteur = GoogleTranslator(source="auto", target=langue_dest)
        morceaux_traduits = []
        for morceau in decouper_texte(texte):
            morceaux_traduits.append(traducteur.translate(morceau))
            time.sleep(0.3)  # petite pause polie entre chaque appel
        return "\n\n".join(morceaux_traduits)
    except Exception as e:
        print(f"  -> Erreur de traduction, texte original conserve : {e}")
        return texte


def charger_json():
    if not os.path.exists(FICHIER_JSON):
        return []
    try:
        with open(FICHIER_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def sauvegarder_json(nouveaux_articles):
    """Fusionne les nouveaux articles avec ceux déjà présents dans
    articles.json, sans doublons (par URL), triés du plus récent au
    plus ancien, et plafonnés à MAX_ARTICLES_JSON entrées."""
    existants = charger_json()
    urls_existantes = {a["url"] for a in existants}

    for titre, date_pub, contenu, url, titre_fr, contenu_fr in nouveaux_articles:
        if url in urls_existantes:
            continue
        existants.append({
            "titre": titre,
            "titre_fr": titre_fr,
            "date": date_pub.isoformat(),
            "url": url,
            "contenu": contenu,
            "contenu_fr": contenu_fr,
        })

    existants.sort(key=lambda a: a["date"], reverse=True)
    existants = existants[:MAX_ARTICLES_JSON]

    with open(FICHIER_JSON, "w", encoding="utf-8") as f:
        json.dump(existants, f, ensure_ascii=False, indent=2)


# ---------- PROGRAMME PRINCIPAL ----------

def cycle():
    """Un seul passage : verifie les nouveaux articles, extrait, sauvegarde."""
    premier_lancement = premiere_execution()

    if premier_lancement:
        fenetre_heures = FENETRE_HEURES_PREMIERE_EXECUTION
        max_pages = MAX_PAGES_PREMIERE_EXECUTION
        print(f"Premier lancement detecte : fenetre de {fenetre_heures // 24} jours, jusqu'a {max_pages} page(s).")
    else:
        fenetre_heures = FENETRE_HEURES
        max_pages = MAX_PAGES_NORMAL

    maintenant = datetime.now(timezone.utc)
    debut_fenetre = maintenant - timedelta(hours=fenetre_heures)

    # Nom du fichier de sortie basé sur la date du jour, ex: 02-09-2026-CB.txt
    nom_fichier = maintenant.strftime("%d-%m-%Y") + "-CB.txt"
    fichier_sortie = os.path.join(DOSSIER_SORTIE, nom_fichier)
    nom_fichier_fr = maintenant.strftime("%d-%m-%Y") + "-CB-FR.txt"
    fichier_sortie_fr = os.path.join(DOSSIER_SORTIE, nom_fichier_fr)

    deja_vus = charger_deja_vus()
    liens = recuperer_liens_articles(fenetre_heures, max_pages)
    candidats = [lien for lien in liens if lien not in deja_vus]

    if not candidats:
        print("Aucun nouvel article a verifier.")
        return

    print(f"{len(candidats)} article(s) a verifier...")

    articles_retenus = []

    for url in candidats:
        try:
            titre, date_pub, contenu = extraire_article(url)

            if date_pub is None:
                print(f"Date introuvable, ignore : {titre}")
                sauvegarder_deja_vu(url)
                continue

            if date_pub < debut_fenetre:
                # Article trop ancien, hors fenetre
                sauvegarder_deja_vu(url)
                continue

            articles_retenus.append((titre, date_pub, contenu, url))
            sauvegarder_deja_vu(url)
            print(f"Retenu : {titre}")

        except Exception as e:
            print(f"Erreur sur {url} : {e}")

        time.sleep(1)  # pause polie entre chaque requete

    if not articles_retenus:
        print("Aucun article dans la fenetre demandee.")
        return

    # Tri du plus ancien au plus recent
    articles_retenus.sort(key=lambda x: x[1])

    with open(fichier_sortie, "a", encoding="utf-8") as f:
        for titre, date_pub, contenu, url in articles_retenus:
            date_affichee = date_pub.strftime("%d-%m-%Y %H:%M UTC")
            f.write("=" * 80 + "\n")
            f.write(f"TITRE : {titre}\n")
            f.write(f"DATE  : {date_affichee}\n")
            f.write(f"URL   : {url}\n")
            f.write("=" * 80 + "\n\n")
            f.write(contenu if contenu else "(Contenu non trouve)")
            f.write("\n\n\n")

    print(f"\nTermine. {len(articles_retenus)} article(s) ecrit(s) dans {nom_fichier}")

    articles_pour_json = []

    if GENERER_VERSION_FR:
        print("Traduction en francais en cours...")
        with open(fichier_sortie_fr, "a", encoding="utf-8") as f:
            for titre, date_pub, contenu, url in articles_retenus:
                date_affichee = date_pub.strftime("%d-%m-%Y %H:%M UTC")
                titre_fr = traduire_texte(titre)
                contenu_fr = traduire_texte(contenu) if contenu else "(Contenu non trouve)"

                f.write("=" * 80 + "\n")
                f.write(f"TITRE : {titre_fr}\n")
                f.write(f"DATE  : {date_affichee}\n")
                f.write(f"URL   : {url}\n")
                f.write("=" * 80 + "\n\n")
                f.write(contenu_fr)
                f.write("\n\n\n")

                articles_pour_json.append((titre, date_pub, contenu, url, titre_fr, contenu_fr))

        print(f"Version francaise ecrite dans {nom_fichier_fr}")
    else:
        for titre, date_pub, contenu, url in articles_retenus:
            articles_pour_json.append((titre, date_pub, contenu, url, titre, contenu))

    sauvegarder_json(articles_pour_json)
    print(f"articles.json mis a jour ({len(articles_pour_json)} nouvel(le)s entree(s)).")


def main_boucle():
    """Boucle infinie : relance un cycle toutes les INTERVALLE_SECONDES,
    des le lancement du script. Une erreur dans un cycle n'arrete pas
    le programme : elle est loguee, puis le script attend et reessaie."""
    print(f"Demarrage. Verification toutes les {INTERVALLE_SECONDES} secondes (Ctrl+C pour arreter).")
    while True:
        debut_cycle = time.time()
        try:
            cycle()
        except Exception as e:
            print(f"Erreur inattendue pendant le cycle : {e}")

        duree_cycle = time.time() - debut_cycle
        attente = max(0, INTERVALLE_SECONDES - duree_cycle)
        time.sleep(attente)


def main():
    if "--once" in sys.argv:
        # Mode utilise par le workflow GitHub Actions : un seul passage,
        # c'est le planificateur (cron) qui se charge de la repetition.
        cycle()
    else:
        main_boucle()


if __name__ == "__main__":
    main()
