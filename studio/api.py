"""Studio 3D à l'échelle — serveur MCP sans session, en plusieurs copies derrière le service « studio ».

Mêmes outils qu'au module s3d01 (soumettre_rendu, etat_rendu, mes_rendus) : pour Open WebUI, rien ne
change. Aucune copie ne garde d'état en mémoire : les demandes vont dans Redis, et les workers
(travailleur.py) viennent les y chercher. Chaque copie surveille aussi les demandes dont le worker a
disparu, et les remet dans la file.

Module s3d03 : chaque outil compte ses appels, et les mesures sont publiées sur /metrics (metriques.py).
Module s3d04 : chaque demande reçoit un identifiant unique et un choix de marquage visible (paramètre
marquage_visible, politique de l'organisation) ; le worker marque le rendu (marquage.py).
Module s3d05 : bibliothèque d'assets (objets 3D, éclairages HDRI) montée en lecture seule ; l'outil
lister_assets la fait découvrir au modèle, la route /a/ publie les vignettes.
Module s3d06 : la scène Blender de chaque rendu est déposée dans le stockage S3 d'une autre équipe.
Le serveur garde les clés longue durée : il prépare les identifiants temporaires de dépôt quand un
worker prend la demande, et répond au lien de la scène par une redirection vers un lien présigné.
"""
import asyncio
import hmac
import json
import time
import uuid

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

import commun as c
import metriques as m
import stockage

r = c.connexion()
m.REGISTRE.register(m.CollecteurFile(r))  # file, rendus en cours, utilisateurs : lus dans Redis à chaque passage de Prometheus
# Sans session : chaque appel se suffit à lui-même, n'importe quelle copie peut y répondre.
mcp = FastMCP("studio", host="0.0.0.0", port=8000, stateless_http=True)


def _entetes(ctx: Context):
    requete = ctx.request_context.request
    return requete.headers if requete is not None else {}


def _utilisateur(ctx: Context) -> str:
    """Adresse de l'utilisateur (en-tête rempli par Open WebUI), après contrôle du jeton du studio."""
    entetes = _entetes(ctx)
    if entetes.get("authorization", "") != f"Bearer {c.JETON}":
        raise PermissionError("jeton du studio invalide")
    email = entetes.get("x-utilisateur-email", "").strip().lower()
    if not email:
        raise PermissionError("utilisateur inconnu : en-tête X-Utilisateur-Email absent")
    # Utilisateurs distincts de l'heure : comptés dans Redis, jamais l'adresse en étiquette.
    heure = m.cle_heure()
    r.pfadd(heure, email)
    r.expire(heure, 7200)
    return email


def _position(numero: int) -> int | None:
    """1 = la prochaine à passer ; None si la demande n'est plus en attente."""
    en_attente = [int(x) for x in r.lrange(c.FILE, 0, -1)]  # gauche = la plus récente
    if numero not in en_attente:
        return None
    return len(en_attente) - en_attente.index(numero)


# ---------------------------------------------------------------- Outils MCP

@mcp.tool()
def soumettre_rendu(titre: str, code: str, ctx: Context,
                    marquage_visible: bool | None = None) -> dict:
    """Met en file d'attente le rendu d'une scène Blender et répond immédiatement.

    code : script Python (bpy) qui construit la scène dans une scène vide : objets, matériaux,
    lumière, caméra. Ne pas régler le moteur ni enregistrer : le studio s'en charge
    (Cycles, 1280 x 720, PNG + modèle 3D GLB). Les objets de décor (sol, table, fond) doivent avoir
    un nom commençant par « Decor » : ils sont rendus mais pas exportés dans le modèle 3D.
    marquage_visible : true pour un bandeau visible sur l'image (source et date),
    seulement si l'utilisateur demande une mention visible ; sinon, ne pas le renseigner.
    Le studio marque toujours le fichier de façon invisible (métadonnées).
    Le studio a une bibliothèque d'objets réels et d'éclairages HDRI : cherchez avec lister_assets,
    puis utilisez charger_asset("nom") et environnement("nom") dans le code, jamais un chemin de fichier.
    Renvoie le numéro de la demande et sa position dans la file."""
    m.DEMANDES.labels(outil="soumettre_rendu").inc()
    utilisateur = _utilisateur(ctx)
    uid = _entetes(ctx).get("x-utilisateur-id", "").strip()  # notifications (série Intégrations)
    # Bandeau : imposé par l'organisation, sinon le choix de l'utilisateur,
    # sinon la politique par défaut.
    choix = marquage_visible if marquage_visible is not None else c.MARQUAGE_DEFAUT
    visible = c.MARQUAGE_IMPOSE or choix
    numero = r.incr("studio:compteur")  # atomique : numéro unique, même avec plusieurs copies
    r.hset(c.demande(numero), mapping={
        "numero": numero, "titre": titre[:80], "code": code, "utilisateur": utilisateur,
        "utilisateur_id": uid, "statut": "en_attente", "soumis": time.time(),
        # Le numéro sert aux humains, l'identifiant à la traçabilité.
        "identifiant": str(uuid.uuid4()),
        "marquage_visible": "1" if visible else "0",
        "scene_prevue": "1" if c.STOCKAGE else "0",  # module s3d06 : la scène Blender sera déposée
    })
    r.lpush(c.FILE, numero)    # la file : les workers la vident par la droite
    r.lpush(c.CHARGE, numero)  # la charge, lue par KEDA : une entrée par demande en attente ou en cours
    suite = "L'utilisateur sera prévenu dans Open WebUI." if uid else "Demandez son état avec etat_rendu."
    return {"numero": numero, "titre": titre, "position": _position(numero), "marquage_visible": visible,
            "message": f"Demande enregistrée. {suite}"}


@mcp.tool()
def etat_rendu(numero: int, ctx: Context) -> dict:
    """Donne l'état d'une demande de l'utilisateur : en_attente (avec sa position), en_cours,
    termine (avec l'image en Markdown et l'artefact 3D à recopier tels quels, et le lien de la scène
    Blender, scene_blend_markdown, à recopier tel quel s'il est présent) ou erreur."""
    m.DEMANDES.labels(outil="etat_rendu").inc()
    utilisateur = _utilisateur(ctx)
    d = {c.texte(k): c.texte(v) for k, v in r.hgetall(c.demande(numero)).items()}
    if not d or d.get("utilisateur") != utilisateur:
        return {"numero": numero, "erreur": "aucune demande de ce numéro pour cet utilisateur"}
    etat = {"numero": numero, "titre": d["titre"], "statut": d["statut"],
            "identifiant": d.get("identifiant", ""), "marquage_visible": d.get("marquage_visible") == "1"}
    if d["statut"] == "en_attente":
        etat["position"] = _position(numero)
    elif d["statut"] == "en_cours":
        etat["depuis_secondes"] = round(time.time() - float(d["debut"]))
        etat["travailleur"] = d.get("travailleur", "")
    elif d["statut"] == "termine":
        etat["duree_secondes"] = round(float(d["fin"]) - float(d["debut"]))
        etat["travailleur"] = d.get("travailleur", "")
        if d.get("assets"):  # assets de la bibliothèque utilisés : nom, source, licence
            etat["assets"] = c.trace_assets(d["assets"].split(","))
        etat["image_markdown"] = f'![{d["titre"]}]({c.lien(numero, "png")})'
        etat["artefact_3d"] = c.artefact(numero, d["titre"])
        if d.get("scene"):  # module s3d06 : la scène Blender, par le lien stable du studio
            etat["scene_blend_markdown"] = f"[{c.LIBELLE_SCENE}]({c.lien(numero, 'blend')})"
            etat["scene_disponible_jusqu_au"] = time.strftime("%Y-%m-%d", time.gmtime(float(d["fin"]) + c.DUREE_SCENES))
        elif d.get("scene_prevue") == "1":
            etat["scene_blend"] = "indisponible"
    else:
        etat["erreur"] = d.get("erreur", "")
    return etat


@mcp.tool()
def mes_rendus(ctx: Context) -> list[dict]:
    """Liste les demandes de l'utilisateur connecté (statut, worker, position), de la plus récente à la plus ancienne."""
    m.DEMANDES.labels(outil="mes_rendus").inc()
    utilisateur = _utilisateur(ctx)
    dernier = int(r.get("studio:compteur") or 0)
    resultats = []
    for n in range(dernier, 0, -1):
        d = {c.texte(k): c.texte(v) for k, v in r.hgetall(c.demande(n)).items()}
        if d.get("utilisateur") == utilisateur:
            ligne = {"numero": n, "titre": d["titre"], "statut": d["statut"], "marquage_visible": d.get("marquage_visible") == "1"}
            if d["statut"] == "en_attente":
                ligne["position"] = _position(n)
            if d.get("travailleur"):
                ligne["travailleur"] = d["travailleur"]
            resultats.append(ligne)
    return resultats


@mcp.tool()
def lister_assets(ctx: Context, recherche: str = "", genre: str = "") -> list[dict]:
    """Cherche dans la bibliothèque d'assets du studio : objets 3D réels (genre « modele ») et
    éclairages HDRI (genre « environnement »). À appeler avant de construire un meuble, un objet ou
    un éclairage : s'il existe, utilisez-le. recherche : un mot (chaise, lampe, studio…), vide pour tout.
    Dans le code de la scène : charger_asset("nom", position=(x, y, z), rotation_z=…) pour un objet
    (posé au sol à cette position, dimensions en mètres : largeur, profondeur, hauteur ; face avant
    tournée vers -y : rotation_z=math.pi pour la tourner vers +y), environnement("nom") pour l'éclairage.
    Pour montrer la bibliothèque à l'utilisateur, recopiez vignette_markdown."""
    m.DEMANDES.labels(outil="lister_assets").inc()
    _utilisateur(ctx)  # même contrôle que les autres outils
    mot = recherche.strip().lower()
    resultats = []
    for famille, fiches in (("modele", c.catalogue()["modeles"]), ("environnement", c.catalogue()["environnements"])):
        if genre and not famille.startswith(genre.strip().lower()[:5]):
            continue
        for nom, f in fiches.items():
            # Recherche dans les noms, en trois langues, et dans les étiquettes.
            texte = " ".join([nom, *f["nom"].values(), *f.get("etiquettes", [])]).lower()
            if mot and mot not in texte:
                continue
            libelle = f["nom"].get(c.LANGUE, f["nom"]["FR"])
            resultats.append({
                "nom": nom,                                  # le nom à écrire dans le code
                "genre": famille,
                "libelle": libelle,
                "dimensions_m": f.get("dimensions_m"),       # pour placer sans deviner
                "vignette_markdown": f"![{libelle}]({c.PUBLIC}/a/{nom}.jpg)",
                "exemple": f'charger_asset("{nom}")' if famille == "modele" else f'environnement("{nom}")',
                "source": "Poly Haven, CC0" if f["source"].get("polyhaven") else f["source"].get("licence", ""),
            })
    return resultats


# ---------------------------------------------------------------- Fichiers (liens signés) et santé

@mcp.custom_route("/r/{numero}/{nom}", methods=["GET"])
async def servir(request: Request) -> Response:
    try:
        numero = int(request.path_params["numero"])
        sig, extension = request.path_params["nom"].rsplit(".", 1)
    except ValueError:
        return Response(status_code=404)
    if extension not in ("png", "glb", "blend") or not hmac.compare_digest(sig, c.signature(numero, extension)):
        return Response(status_code=404)
    if extension == "blend":  # module s3d06 : redirection vers le stockage, lien de soixante secondes
        return await _scene(numero)
    contenu = r.get(c.fichier(numero, extension))
    if contenu is None:
        return Response(status_code=404)
    return Response(contenu, media_type="image/png" if extension == "png" else "model/gltf-binary",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "private, max-age=3600"})


async def _scene(numero: int) -> Response:
    d = {c.texte(k): c.texte(v) for k, v in r.hgetall(c.demande(numero)).items()}
    if not d.get("scene") or time.time() > float(d.get("fin") or 0) + c.DUREE_SCENES:
        m.LIENS_SCENE.labels(resultat="expiree").inc()
        return Response(status_code=404)
    try:
        url = await asyncio.to_thread(stockage.lien_presigne, d)  # identifiants de lecture, puis signature
    except Exception as e:  # stockage injoignable : le reste du studio continue
        print(f"lien de scène {numero} : {e}")
        m.LIENS_SCENE.labels(resultat="indisponible").inc()
        return Response(status_code=503)
    m.LIENS_SCENE.labels(resultat="redirige").inc()
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/a/{fichier}", methods=["GET"])
async def vignette(request: Request) -> Response:
    """Vignettes de la bibliothèque : publiques, sans signature (assets libres de droits, aucune donnée d'utilisateur)."""
    nom, _, extension = request.path_params["fichier"].rpartition(".")
    cat = c.catalogue()
    fiche = cat["modeles"].get(nom) or cat["environnements"].get(nom)  # nom cherché, jamais concaténé tel quel
    if extension != "jpg" or fiche is None:
        return Response(status_code=404)
    return FileResponse(c.ASSETS / fiche["vignette"], media_type="image/jpeg",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/metrics", methods=["GET"])
async def metriques(request: Request) -> Response:
    """Mesures au format Prometheus : interne au cluster (aucun Ingress ne publie /metrics)."""
    return Response(m.generate_latest(m.REGISTRE), media_type=m.CONTENT_TYPE_LATEST)


@mcp.custom_route("/sante", methods=["GET"])
async def sante(request: Request) -> Response:
    return JSONResponse({"ok": bool(r.ping()), "en_attente": r.llen(c.FILE), "en_traitement": r.llen(c.TRAITEMENT), "charge": r.llen(c.CHARGE)})


# Routes de la série Intégrations (widget de suivi, fonction Pipe) : fichier à part, absent du dépôt du module s3d02.
try:
    import integrations
    integrations.brancher(mcp, r, _position)
except ImportError:
    pass


# ---------------------------------------------------------------- Demandes orphelines

async def _surveiller() -> None:
    """Remet dans la file les demandes dont le worker ne bat plus (pod supprimé, arrêt brutal)."""
    suspects: set[int] = set()  # demandes sans battement à la ronde précédente
    while True:
        await asyncio.sleep(3)
        try:
            for brut in r.lrange(c.TRAITEMENT, 0, -1):
                n = int(brut)
                if r.exists(c.vivant(n)):  # le worker bat : tout va bien
                    suspects.discard(n)
                    continue
                if n not in suspects:      # première ronde sans battement : on attend la suivante
                    suspects.add(n)
                    continue
                suspects.discard(n)
                if r.lrem(c.TRAITEMENT, 1, brut):  # une seule copie du serveur gagne
                    r.hset(c.demande(n), mapping={"statut": "en_attente", "travailleur": ""})
                    r.rpush(c.FILE, n)  # à droite : elle repasse en premier
        except Exception as e:  # Redis indisponible un instant : on réessaie
            print(f"surveillance : {e}")


# ---------------------------------------------------------------- Identifiants de dépôt (module s3d06)

async def _preparer_depots() -> None:
    """Dès qu'un worker prend une demande, des identifiants de dépôt pour elle seule (quinze minutes).
    Le worker ne voit jamais la clé longue durée : elle reste dans le serveur MCP."""
    while True:
        await asyncio.sleep(2)
        try:
            en_cours = r.lrange(c.TRAITEMENT, 0, -1)
        except Exception:  # Redis indisponible un instant : on réessaie
            continue
        for brut in en_cours:
            n = int(brut)
            try:
                # Une seule copie du serveur s'en charge (SET NX), une seule fois par demande.
                if r.hget(c.demande(n), "scene_prevue") != b"1" or not r.set(f"studio:depot-prepare:{n}", 1, nx=True, ex=c.DUREE_IDENTIFIANTS):
                    continue
                d = {c.texte(k): c.texte(v) for k, v in r.hgetall(c.demande(n)).items()}
                ident = await asyncio.to_thread(stockage.preparer_depot, d)
                r.setex(c.depot_s3(n), c.DUREE_IDENTIFIANTS, json.dumps(ident))
            except Exception as e:  # stockage injoignable : on réessaiera à la ronde suivante
                r.delete(f"studio:depot-prepare:{n}")
                print(f"identifiants de dépôt {n} : {e}")


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    demarrage = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def cycle(application):
        taches = [asyncio.create_task(_surveiller())]
        if c.STOCKAGE:
            taches.append(asyncio.create_task(_preparer_depots()))
        async with demarrage(application):
            yield
        for tache in taches:
            tache.cancel()

    app.router.lifespan_context = cycle
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
