"""Studio 3D à l'échelle — mesures Prometheus, partagées par le serveur MCP (api.py) et les workers (travailleur.py).

Le serveur les publie sur /metrics (port 8000), chaque worker sur le port 9101. Prometheus découvre
les pods par leurs annotations (k8s-echelle.yaml) et vient les lire toutes les cinq secondes.
"""
import time

from prometheus_client import CollectorRegistry, Counter, Histogram, disable_created_metrics
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, start_http_server  # noqa: F401 (pour api.py et travailleur.py)
from prometheus_client.core import GaugeMetricFamily

import commun as c

disable_created_metrics()  # pas de séries « _created » : la page reste lisible
REGISTRE = CollectorRegistry()

# Une seule étiquette : l'outil appelé. Jamais l'adresse de l'utilisateur (une série par adresse, et une donnée personnelle).
DEMANDES = Counter("studio_demandes", "Appels reçus, par outil MCP", ["outil"], registry=REGISTRE)  # publié : studio_demandes_total
for _outil in ("soumettre_rendu", "etat_rendu", "mes_rendus", "lister_assets"):
    DEMANDES.labels(outil=_outil)  # séries créées à zéro : une courbe par outil dès le démarrage

# Paliers en secondes : un rendu prend de vingt secondes (seul) à plus d'une minute (quatre en parallèle).
PALIERS = (10, 20, 30, 45, 60, 90, 120, 180, 300)
DUREE = Histogram("studio_rendu_duree_secondes", "Durée d'un rendu Blender", buckets=PALIERS, registry=REGISTRE)
ATTENTE = Histogram("studio_attente_secondes", "Attente dans la file avant le rendu", buckets=PALIERS, registry=REGISTRE)
RENDUS = Counter("studio_rendus", "Rendus finis, par résultat : termine, erreur, interrompu", ["resultat"], registry=REGISTRE)
for _resultat in ("termine", "erreur", "interrompu"):
    RENDUS.labels(resultat=_resultat)
# Module s3d05 : assets utilisés par les rendus. Étiquette bornée : seuls les noms du catalogue.
ASSETS_UTILISES = Counter("studio_assets_utilises", "Assets de la bibliothèque utilisés par les rendus", ["asset"], registry=REGISTRE)
# Module s3d06 : scènes Blender déposées dans le stockage de l'autre équipe (étiquettes bornées).
SCENES = Counter("studio_scenes", "Scènes Blender, par résultat : deposee, indisponible, trop_grande", ["resultat"], registry=REGISTRE)
for _resultat in ("deposee", "indisponible", "trop_grande"):
    SCENES.labels(resultat=_resultat)
SCENES_OCTETS = Counter("studio_scenes_octets", "Octets des scènes déposées", registry=REGISTRE)
LIENS_SCENE = Counter("studio_liens_scene", "Liens de scène suivis, par résultat : redirige, expiree, indisponible", ["resultat"], registry=REGISTRE)
for _resultat in ("redirige", "expiree", "indisponible"):
    LIENS_SCENE.labels(resultat=_resultat)
IDENTIFIANTS = Counter("studio_identifiants_temporaires", "Identifiants temporaires obtenus du stockage, par usage : depot, lecture", ["usage"], registry=REGISTRE)
for _usage in ("depot", "lecture"):
    IDENTIFIANTS.labels(usage=_usage)


def cle_heure(t: float | None = None) -> str:
    """Clé Redis des utilisateurs distincts de l'heure (HyperLogLog, expire en deux heures)."""
    return "studio:utilisateurs:" + time.strftime("%Y%m%d%H", time.gmtime(t))


class CollecteurFile:
    """Lu au passage de Prometheus : valeurs fraîches, qu'aucune copie du serveur n'a à tenir à jour."""

    def __init__(self, r):
        self.r = r

    def collect(self):
        yield GaugeMetricFamily("studio_file_attente", "Demandes en attente dans la file", value=self.r.llen(c.FILE))
        yield GaugeMetricFamily("studio_rendus_en_cours", "Demandes prises par un worker", value=self.r.llen(c.TRAITEMENT))
        yield GaugeMetricFamily("studio_utilisateurs_heure", "Utilisateurs distincts sur l'heure en cours", value=self.r.pfcount(cle_heure()))

