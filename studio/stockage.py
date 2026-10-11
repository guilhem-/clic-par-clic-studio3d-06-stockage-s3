"""Stockage des scènes Blender (module s3d06) : le service S3 d'une autre équipe, espace « stockage ».

Le studio ne parle que S3. Ses deux clés longue durée (dépôt, liens) restent dans le serveur MCP, et ne
servent qu'à une chose : obtenir des identifiants temporaires (STS AssumeRole), limités au dossier de
l'utilisateur. Le worker, où s'exécute le code écrit par le modèle, ne reçoit que ceux-là.
"""
import hashlib
import hmac
import json
import re
import time
import unicodedata

import boto3
from botocore.config import Config

import commun as c
import metriques as m

REGLAGES = Config(signature_version="s3v4", s3={"addressing_style": "path"},  # chemin, pas sous-domaine
                  connect_timeout=3, read_timeout=60, retries={"max_attempts": 3, "mode": "standard"},
                  request_checksum_calculation="when_required", response_checksum_validation="when_required")


def _client(service, adresse, cle, secret, jeton=None):
    return boto3.client(service, endpoint_url=adresse, region_name=c.S3_REGION, aws_access_key_id=cle,
                        aws_secret_access_key=secret, aws_session_token=jeton, config=REGLAGES)


def pseudonyme(email: str) -> str:
    """Un dossier par utilisateur, sans son adresse : l'équipe stockage n'a pas à la voir."""
    return hmac.new(c.SECRET, email.encode(), hashlib.sha256).hexdigest()[:16]


def cle_objet(d: dict) -> str:
    return f"scenes/{pseudonyme(d['utilisateur'])}/{d['identifiant']}.blend"


def identifiants(cle: str, secret: str, actions: list, ressource: str, usage: str) -> dict:
    """Échange une clé longue durée contre des identifiants de quinze minutes, limités à `ressource`."""
    politique = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{c.BUCKET}/{ressource}"]}]}
    r = _client("sts", c.S3_INTERNE, cle, secret).assume_role(
        RoleArn="arn:aws:iam::000000000000:role/studio3d", RoleSessionName="studio3d",
        DurationSeconds=c.DUREE_IDENTIFIANTS, Policy=json.dumps(politique))["Credentials"]
    m.IDENTIFIANTS.labels(usage=usage).inc()
    return {"cle": r["AccessKeyId"], "secret": r["SecretAccessKey"], "jeton": r["SessionToken"],
            "expire": r["Expiration"].timestamp()}


def preparer_depot(d: dict) -> dict:
    """Serveur MCP : le droit d'écrire ce seul fichier, dans le dossier de l'utilisateur."""
    return identifiants(c.S3_DEPOT_CLE, c.S3_DEPOT_SECRET, ["s3:PutObject"], cle_objet(d), "depot")


def nom_fichier(d: dict) -> str:
    """Nom proposé au téléchargement : le titre en ASCII, puis le numéro (salon-1.blend)."""
    titre = unicodedata.normalize("NFKD", d.get("titre", "scene")).encode("ascii", "ignore").decode()
    titre = re.sub(r"[^a-z0-9]+", "-", titre.lower()).strip("-")[:40].rstrip("-") or "scene"
    return f"{titre}-{d.get('numero', '')}.blend"


def deposer(chemin, d: dict, i: dict, ident: dict) -> dict:
    """Worker : dépose la scène avec les identifiants temporaires préparés par le serveur MCP."""
    taille = chemin.stat().st_size
    if taille > c.SCENE_TAILLE_MAX:
        raise ValueError("trop_grande")
    s3 = _client("s3", c.S3_INTERNE, ident["cle"], ident["secret"], ident["jeton"])
    with chemin.open("rb") as f:
        rep = s3.put_object(
            Bucket=c.BUCKET, Key=cle_objet(d), Body=f, ContentLength=taille,
            ContentType="application/x-blender",
            ContentDisposition=f'attachment; filename="{nom_fichier(d)}"',
            Metadata={"digital-source-type": "trainedAlgorithmicMedia", "generateur": "AI studio3D",
                      "identifiant": f"urn:uuid:{i['identifiant']}", "numero": str(d.get("numero", "")),
                      "assets": ",".join(a["nom"] for a in i.get("assets", []))[:500]})
    return {"cle": cle_objet(d), "version": rep.get("VersionId", ""), "octets": taille}


_lecture: dict = {}  # identifiants de lecture, par pseudonyme (gardés tant qu'il leur reste deux minutes)


def lien_presigne(d: dict) -> str:
    """Serveur MCP : lien de soixante secondes, signé pour l'adresse PUBLIQUE (la signature couvre le
    nom d'hôte), avec des identifiants de lecture limités au dossier de cet utilisateur."""
    pseudo = pseudonyme(d["utilisateur"])
    ident = _lecture.get(pseudo)
    if not ident or ident["expire"] - time.time() < 120:
        ident = _lecture[pseudo] = identifiants(c.S3_LIENS_CLE, c.S3_LIENS_SECRET,
                                                ["s3:GetObject", "s3:GetObjectVersion"], f"scenes/{pseudo}/*", "lecture")
    params = {"Bucket": c.BUCKET, "Key": d["scene"]}
    if d.get("scene_version"):
        params["VersionId"] = d["scene_version"]  # la version déposée par le worker, pas une autre
    # Signer est un calcul local : aucun appel réseau vers l'adresse publique.
    return _client("s3", c.S3_PUBLIC, ident["cle"], ident["secret"], ident["jeton"]).generate_presigned_url(
        "get_object", Params=params, ExpiresIn=c.DUREE_LIEN_S3)
