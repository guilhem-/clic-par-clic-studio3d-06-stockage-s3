"""Studio 3D à l'échelle : ce que partagent le serveur MCP (api.py) et les workers (travailleur.py).

Tout l'état vit dans Redis, pour que le serveur tourne en plusieurs copies sans session et que
les workers aillent eux-mêmes chercher les demandes :

  studio:compteur              numéro de la prochaine demande (INCR)
  studio:demande:<n>           hash : titre, code, utilisateur, utilisateur_id, statut, soumis, debut, fin, erreur, travailleur,
                               identifiant (UUID, traçabilité) et marquage_visible (« 1 » ou « 0 », module s3d04),
                               assets (noms des assets utilisés, séparés par des virgules) et bibliotheque (version, module s3d05),
                               scene_prevue, scene (clé de l'objet S3), scene_version, scene_octets, scene_statut (module s3d06)
  studio:file                  demandes en attente (LPUSH à gauche, prises à droite)
  studio:traitement            demandes prises par un worker (BLMOVE file → traitement)
  studio:charge                une entrée par demande en attente ou en cours : la mesure de KEDA
  studio:vivant:<n>            battement de cœur du worker qui traite <n> (expire en 10 s)
  studio:fichier:<n>:<ext>     PNG et GLB produits (expirent en 24 h ; en production : stockage objet)
  studio:depot:<n>             identifiants temporaires de dépôt de la scène <n> (module s3d06, expirent en 15 min)
  studio:notifications         rendus finis à annoncer (série Intégrations, lus par le notificateur)
  studio:utilisateurs:<heure>  utilisateurs distincts de l'heure (HyperLogLog, expire en 2 h ; seul le nombre est publié, module s3d03)
"""
import hashlib
import hmac
import json
import os
from functools import lru_cache
from pathlib import Path

import redis

JETON = os.environ.get("STUDIO_JETON", "")
SECRET = os.environ.get("STUDIO_SECRET", JETON).encode()
PUBLIC = os.environ.get("STUDIO_PUBLIC_URL", "http://studio.localhost:8080").rstrip("/")
DUREE_FICHIERS = int(os.environ.get("DUREE_FICHIERS", str(24 * 3600)))

# Marquage des rendus (module s3d04) : politique de l'organisation, lue dans l'environnement (oui / non).
#   MARQUAGE_VISIBLE_DEFAUT : bandeau visible quand l'utilisateur ne précise rien
#   MARQUAGE_VISIBLE_IMPOSE : bandeau visible toujours, quoi que demande l'utilisateur
# Le marquage invisible (métadonnées), lui, est toujours fait.
_oui = lambda nom: os.environ.get(nom, "non").strip().lower() in ("oui", "1", "true", "yes")
MARQUAGE_DEFAUT = _oui("MARQUAGE_VISIBLE_DEFAUT")
MARQUAGE_IMPOSE = _oui("MARQUAGE_VISIBLE_IMPOSE")

# Bibliothèque d'assets (module s3d05) : une image montée en lecture seule sur ce dossier.
ASSETS = Path(os.environ.get("ASSETS", "/assets"))
LANGUE = os.environ.get("STUDIO_LANGUE", "FR")  # langue des libellés renvoyés au modèle

# Stockage des scènes Blender (module s3d06) : le service S3 d'une autre équipe. Facultatif : sans les
# clés (secret « stockage » absent), le studio se comporte comme aux modules précédents.
S3_INTERNE = os.environ.get("S3_INTERNE", "")      # adresse vue des pods (dépôts, identifiants temporaires)
S3_PUBLIC = os.environ.get("S3_PUBLIC", "")        # adresse vue du navigateur (liens présignés)
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
BUCKET = os.environ.get("S3_BUCKET", "studio3d-scenes")
S3_DEPOT_CLE, S3_DEPOT_SECRET = os.environ.get("S3_DEPOT_CLE", ""), os.environ.get("S3_DEPOT_SECRET", "")
S3_LIENS_CLE, S3_LIENS_SECRET = os.environ.get("S3_LIENS_CLE", ""), os.environ.get("S3_LIENS_SECRET", "")
STOCKAGE = bool(S3_INTERNE and S3_PUBLIC and S3_DEPOT_CLE and S3_LIENS_CLE)  # serveur MCP : clés reçues
DUREE_SCENES = int(os.environ.get("DUREE_SCENES", str(7 * 24 * 3600)))       # comme le cycle de vie du bucket
DUREE_LIEN_S3 = int(os.environ.get("DUREE_LIEN_S3", "60"))                    # lien présigné : une minute
DUREE_IDENTIFIANTS = 900                                                      # minimum de STS : quinze minutes
SCENE_TAILLE_MAX = int(os.environ.get("SCENE_TAILLE_MAX", str(200 * 1024 * 1024)))
LIBELLE_SCENE = {"FR": "Télécharger la scène Blender", "EN": "Download the Blender scene",
                 "DE": "Blender-Szene herunterladen"}.get(LANGUE, "Télécharger la scène Blender")


@lru_cache(maxsize=1)
def catalogue() -> dict:
    """Catalogue de la bibliothèque montée (lu une fois : un pod garde la version montée à sa naissance)."""
    fiche = ASSETS / "catalogue.json"
    if not fiche.exists():
        return {"version": None, "modeles": {}, "environnements": {}}
    return json.loads(fiche.read_text(encoding="utf-8"))


def trace_assets(noms) -> list[dict]:
    """Ce que le studio garde de chaque asset utilisé : nom, source, licence (seuls les noms du catalogue)."""
    cat = catalogue()
    fiches = {**cat.get("modeles", {}), **cat.get("environnements", {})}
    return [{"nom": n, "source": f"Poly Haven {fiches[n]['source']['polyhaven']}" if fiches[n]["source"].get("polyhaven")
             else fiches[n]["source"].get("origine", "organisation"), "licence": fiches[n]["source"].get("licence", "")}
            for n in noms if n in fiches]


FILE, TRAITEMENT, CHARGE, NOTIFICATIONS = "studio:file", "studio:traitement", "studio:charge", "studio:notifications"
demande = lambda n: f"studio:demande:{n}"
vivant = lambda n: f"studio:vivant:{n}"
fichier = lambda n, ext: f"studio:fichier:{n}:{ext}"
depot_s3 = lambda n: f"studio:depot:{n}"


def connexion() -> redis.Redis:
    return redis.Redis(host=os.environ.get("REDIS_HOTE", "redis"), port=6379,
                       password=os.environ.get("REDIS_MOT_DE_PASSE") or None, decode_responses=False)


def texte(valeur) -> str:
    return valeur.decode() if isinstance(valeur, bytes) else (valeur or "")


def signature(numero: int, extension: str) -> str:
    return hmac.new(SECRET, f"{numero}.{extension}".encode(), hashlib.sha256).hexdigest()[:10]


def lien(numero: int, extension: str) -> str:
    return f"{PUBLIC}/r/{numero}/{signature(numero, extension)}.{extension}"


def artefact(numero: int, titre: str) -> str:
    return (
        '```html\n'
        '<script type="module" src="https://cdn.jsdelivr.net/npm/@google/model-viewer@4.0.0/dist/model-viewer.min.js"></script>\n'
        f'<model-viewer src="{lien(numero, "glb")}" poster="{lien(numero, "png")}" alt="{titre}" '
        'camera-controls auto-rotate shadow-intensity="1" '
        'style="width:100%;height:480px;background:#f4f4f5"></model-viewer>\n'
        '```'
    )
