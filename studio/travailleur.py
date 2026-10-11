"""Studio 3D à l'échelle — worker Blender (plusieurs copies, nombre piloté par KEDA).

Va chercher une demande dans la file quand il est libre (BLMOVE file → traitement), la rend avec un
Blender neuf (rendu.py), range les fichiers dans Redis, puis recommence. Pendant le rendu, il bat
(studio:vivant:<n>, toutes les 3 s, expire en 10 s) : s'il disparaît, le serveur remet la demande dans la file. À l'arrêt demandé
(SIGTERM, réduction du nombre de workers), il termine son rendu en cours puis s'arrête.

Module s3d03 : il publie ses mesures sur le port 9101 (durée des rendus, attente, résultats : metriques.py).
Module s3d04 : il marque chaque rendu après Blender, avant de le ranger (marquage.py) ; une erreur de
marquage met la demande en erreur : aucun fichier non marqué ne sort.
Module s3d05 : Blender lit la bibliothèque d'assets (dossier ASSETS, en lecture seule) ; les assets
utilisés sont rangés avec la demande, comptés, et ajoutés au marquage.
Module s3d06 : si le serveur MCP a prévu la scène, un Blender neuf la nettoie, la marque et l'empaquette
(empaqueter.py), puis le worker la dépose dans le stockage S3 de l'autre équipe, avec les identifiants
temporaires préparés par le serveur. Un échec du stockage ne fait pas échouer le rendu.
"""
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import commun as c
import marquage
import metriques as m
import stockage

BLENDER = os.environ.get("BLENDER", "/opt/blender/blender")
RENDU = Path(__file__).with_name("rendu.py")
EMPAQUETER = Path(__file__).with_name("empaqueter.py")
DUREE_MAX = int(os.environ.get("DUREE_MAX", "300"))
NOM = socket.gethostname()  # nom du pod
r = c.connexion()
arret = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: arret.set())


def battre(numero: int, fini: threading.Event) -> None:
    """Battement de cœur : tant que le rendu dure, une clé qui expire en 10 s, renouvelée toutes les 3 s."""
    while not fini.wait(3):
        r.setex(c.vivant(numero), 10, NOM)


def rendre(numero: int) -> None:
    d = {c.texte(k): c.texte(v) for k, v in r.hgetall(c.demande(numero)).items()}
    m.ATTENTE.observe(max(0.0, time.time() - float(d.get("soumis") or time.time())))  # temps passé dans la file
    r.setex(c.vivant(numero), 10, NOM)
    r.hset(c.demande(numero), mapping={"statut": "en_cours", "debut": time.time(), "travailleur": NOM})
    fini = threading.Event()
    threading.Thread(target=battre, args=(numero, fini), daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dossier = Path(tmp)
            (dossier / "scene.py").write_text(d.get("code", ""), encoding="utf-8")
            # Le code du modèle s'exécute dans ce Blender, sans les variables d'environnement du worker.
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "ASSETS": str(c.ASSETS)}  # sauf la bibliothèque
            scene = d.get("scene_prevue") == "1"  # module s3d06 : décidé par le serveur MCP
            if scene:
                env["SCENE_BLEND"] = "1"
            with m.DUREE.time():  # chronomètre autour du rendu, réussi ou non
                sortie = subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(RENDU), "--", str(dossier)],
                                        capture_output=True, text=True, timeout=DUREE_MAX, env=env).stdout
            resultat = dossier / "resultat.json"
            if not resultat.exists():
                raise RuntimeError("\n".join(sortie.strip().splitlines()[-12:]) or "Blender n'a rien produit")
            info = json.loads(resultat.read_text(encoding="utf-8"))
            if info.get("erreur"):
                raise RuntimeError(info["erreur"])
            # Assets de la bibliothèque utilisés : la trace (seuls les noms du catalogue comptent).
            assets = c.trace_assets(info.get("assets", []))
            for a in assets:
                m.ASSETS_UTILISES.labels(asset=a["nom"]).inc()
            r.hset(c.demande(numero), mapping={"assets": ",".join(a["nom"] for a in assets),
                                               "bibliotheque": str(info.get("bibliotheque") or "")})
            # Marquage par notre code, après Blender : toujours dans les fichiers,
            # bandeau si demandé. Une erreur ici : rendu en erreur, rien ne sort.
            i = marquage.infos(d, time.time(), assets, info.get("bibliotheque"))
            visible = d.get("marquage_visible") == "1"
            marquage.marquer_png(dossier / "rendu.png", i, visible)
            marquage.marquer_glb(dossier / "modele.glb", i)
            r.setex(c.fichier(numero, "png"), c.DUREE_FICHIERS, (dossier / "rendu.png").read_bytes())
            r.setex(c.fichier(numero, "glb"), c.DUREE_FICHIERS, (dossier / "modele.glb").read_bytes())
            if scene:
                deposer_scene(numero, d, i, dossier, env)
        r.hset(c.demande(numero), mapping={"statut": "termine", "fin": time.time()})
        m.RENDUS.labels(resultat="termine").inc()
    except subprocess.TimeoutExpired:
        r.hset(c.demande(numero), mapping={"statut": "erreur", "fin": time.time(), "erreur": f"rendu interrompu après {DUREE_MAX} s"})
        m.RENDUS.labels(resultat="interrompu").inc()
    except Exception as e:  # le message revient au modèle, qui peut corriger et resoumettre
        r.hset(c.demande(numero), mapping={"statut": "erreur", "fin": time.time(), "erreur": str(e)[-1500:]})
        m.RENDUS.labels(resultat="erreur").inc()
    finally:
        fini.set()
        r.lrem(c.TRAITEMENT, 1, numero)
        r.lrem(c.CHARGE, 1, numero)  # demande finie (réussie ou en erreur) : KEDA peut réduire
        r.delete(c.vivant(numero))
        if d.get("utilisateur_id"):  # notifications (série Intégrations) : le notificateur préviendra l'utilisateur
            r.lpush(c.NOTIFICATIONS, numero)


def deposer_scene(numero: int, d: dict, i: dict, dossier: Path, env: dict) -> None:
    """Module s3d06 : la scène Blender, nettoyée et empaquetée, déposée dans le stockage de l'autre équipe."""
    try:
        (dossier / "marquage.json").write_text(json.dumps(i, ensure_ascii=False), encoding="utf-8")
        # Un Blender neuf, qui ouvre le fichier sans en exécuter les scripts (--disable-autoexec).
        subprocess.run([BLENDER, "-b", "--factory-startup", "--disable-autoexec", str(dossier / "brut.blend"),
                        "--python", str(EMPAQUETER), "--", str(dossier)], capture_output=True, timeout=120, env=env)
        if not (dossier / "scene.blend").exists():
            raise RuntimeError("scène non empaquetée")
        # Identifiants temporaires préparés par le serveur MCP : ce seul fichier, quinze minutes.
        ident = None
        for _ in range(12):  # prêts depuis longtemps, sauf si le stockage est injoignable
            ident = r.get(c.depot_s3(numero))
            if ident:
                break
            time.sleep(1)
        if not ident:
            raise RuntimeError("aucun identifiant de dépôt")
        depot = stockage.deposer(dossier / "scene.blend", d, i, json.loads(ident))
        r.hset(c.demande(numero), mapping={"scene": depot["cle"], "scene_version": depot["version"],
                                           "scene_octets": depot["octets"], "scene_statut": "deposee"})
        m.SCENES.labels(resultat="deposee").inc()
        m.SCENES_OCTETS.inc(depot["octets"])
    except Exception as e:  # stockage absent, refus, délai dépassé : le rendu reste réussi
        resultat = "trop_grande" if str(e) == "trop_grande" else "indisponible"
        r.hset(c.demande(numero), mapping={"scene_statut": f"{resultat} : {str(e)[:200]}"})
        m.SCENES.labels(resultat=resultat).inc()
    finally:
        r.delete(c.depot_s3(numero))


def main() -> None:
    m.start_http_server(9101, registry=m.REGISTRE)  # page des mesures, lue par Prometheus
    print(f"{NOM} prêt", flush=True)
    while not arret.is_set():  # arrêt demandé (SIGTERM) : on finit le rendu en cours, puis on sort
        # Attend une demande 2 s au plus, et la déplace d'un seul geste de la file vers « en cours ».
        brut = r.blmove(c.FILE, c.TRAITEMENT, 2, "RIGHT", "LEFT")
        if brut is None:
            continue
        numero = int(brut)
        print(f"{NOM} : demande {numero}", flush=True)
        rendre(numero)
    print(f"{NOM} arrêté", flush=True)


if __name__ == "__main__":
    main()
