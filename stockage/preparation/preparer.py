"""Préparation du stockage S3 par l'équipe stockage (module s3d06). Idempotent : relancé sans risque.

  python preparer.py            bucket, versionnement, cycle de vie, politiques, comptes, contrôle final
  python preparer.py --vider    vide le dossier scenes/ (toutes les versions)
  python preparer.py --essai    tests de recevabilité (identifiants temporaires, refus attendus)

Lancé par un Job Kubernetes dans l'espace « stockage », avec la clé racine (secret s3-racine) et les
clés des comptes (secret comptes). Fichiers lus dans /config : politiques/*.json, cycle-de-vie.json.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

S3 = os.environ.get("S3_INTERNE", "http://s3.stockage.svc.cluster.local:9000")
BUCKET = os.environ.get("BUCKET", "studio3d-scenes")
CONFIG = Path(os.environ.get("CONFIG", "/config"))
REGLAGES = Config(signature_version="s3v4", s3={"addressing_style": "path"},
                  request_checksum_calculation="when_required", response_checksum_validation="when_required")
# Trois comptes, trois politiques ; la clé racine n'est jamais remise au studio.
COMPTES = {"studio3d-depot": "DEPOT", "studio3d-liens": "LIENS", "studio3d-consultation": "CONSULTATION"}


def client(cle, secret, jeton=None, service="s3"):
    return boto3.client(service, endpoint_url=S3, region_name="us-east-1", aws_access_key_id=cle,
                        aws_secret_access_key=secret, aws_session_token=jeton, config=REGLAGES)


def racine():
    return client(os.environ["RACINE_CLE"], os.environ["RACINE_SECRET"])


def rc(*args):
    """Client rc de RustFS (comptes et politiques) ; la sortie est résumée à une ligne."""
    p = subprocess.run(["rc", "--no-color", *args], capture_output=True, text=True)
    sortie = (p.stdout + p.stderr).strip().splitlines()
    return p.returncode, (sortie[-1] if sortie else "")


def etape(texte):
    print(f"  {texte}", flush=True)


def attendre():
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{S3}/health", timeout=2):
                return
        except Exception:
            time.sleep(2)
    raise SystemExit("le service s3 ne répond pas")


def preparer():
    s3 = racine()
    try:
        s3.head_bucket(Bucket=BUCKET)
        etape(f"bucket {BUCKET} : déjà là")
    except ClientError:
        s3.create_bucket(Bucket=BUCKET)
        etape(f"bucket {BUCKET} : créé")
    # Versionnement : un objet remplacé garde sa version d'origine (le studio lie chaque lien à la sienne).
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    etape("versionnement : activé")
    # Cycle de vie : scènes effacées au bout de 7 jours, envois inachevés au bout d'un jour.
    regles = json.loads((CONFIG / "cycle-de-vie.json").read_text(encoding="utf-8"))
    s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration=regles)
    etape("cycle de vie : " + ", ".join(r["ID"] for r in regles["Rules"]))

    # Comptes et politiques, avec la clé racine (alias « racine » du client rc).
    rc("alias", "set", "racine", S3, os.environ["RACINE_CLE"], os.environ["RACINE_SECRET"])
    for compte, prefixe in COMPTES.items():
        politique = CONFIG / "politiques" / f"{compte.removeprefix('studio3d-')}.json"
        code, msg = rc("admin", "policy", "create", "racine", compte, str(politique))
        assert code == 0, msg
        code, msg = rc("admin", "user", "add", "racine", compte, os.environ[f"{prefixe}_SECRET"])
        assert code == 0, msg
        code, msg = rc("admin", "policy", "attach", "racine", compte, "--user", compte)
        assert code == 0 or "already" in msg.lower(), msg
        etape(f"compte {compte} : politique {compte}")

    # Contrôle final : ce qui est réellement en place.
    relu = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    etape(f"contrôle : {len(relu)} règles de cycle de vie, versionnement "
          f"{s3.get_bucket_versioning(Bucket=BUCKET).get('Status')}")


def vider():
    s3 = racine()
    n = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=BUCKET, Prefix="scenes/"):
        objets = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        if objets:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objets, "Quiet": True})
            n += len(objets)
    etape(f"scenes/ vidé : {n} versions effacées")


def essai():
    """Recevabilité : les clés longues ne servent qu'à obtenir des identifiants temporaires."""
    def tente(nom, f, attendu):
        try:
            f()
            obtenu = "accepté"
        except ClientError as e:
            obtenu = "refusé" if e.response["Error"]["Code"] in ("AccessDenied", "403") else e.response["Error"]["Code"]
        print(f"  {'ok ' if obtenu == attendu else 'KO '} {nom} : {obtenu}", flush=True)
        return obtenu == attendu

    def temporaires(compte, actions, ressource):
        sts = client(compte, os.environ[f"{COMPTES[compte]}_SECRET"], service="sts")
        politique = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": actions, "Resource": [ressource]}]}
        c = sts.assume_role(RoleArn="arn:aws:iam::000000000000:role/studio3d", RoleSessionName="essai",
                            DurationSeconds=900, Policy=json.dumps(politique))["Credentials"]
        return client(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])

    cle = "scenes/essai0000/essai.blend"
    depot_long = client("studio3d-depot", os.environ["DEPOT_SECRET"])
    liens_long = client("studio3d-liens", os.environ["LIENS_SECRET"])
    depot = temporaires("studio3d-depot", ["s3:PutObject"], f"arn:aws:s3:::{BUCKET}/{cle}")
    liens = temporaires("studio3d-liens", ["s3:GetObject", "s3:GetObjectVersion"], f"arn:aws:s3:::{BUCKET}/scenes/essai0000/*")
    resultats = [
        tente("clé longue de dépôt, écriture directe", lambda: depot_long.put_object(Bucket=BUCKET, Key=cle, Body=b"x"), "refusé"),
        tente("dépôt temporaire, le fichier prévu", lambda: depot.put_object(Bucket=BUCKET, Key=cle, Body=b"x"), "accepté"),
        tente("dépôt temporaire, autre fichier", lambda: depot.put_object(Bucket=BUCKET, Key="scenes/essai0000/autre.blend", Body=b"x"), "refusé"),
        tente("dépôt temporaire, autre utilisateur", lambda: depot.put_object(Bucket=BUCKET, Key="scenes/autre000/essai.blend", Body=b"x"), "refusé"),
        tente("dépôt temporaire, lecture", lambda: depot.get_object(Bucket=BUCKET, Key=cle), "refusé"),
        tente("dépôt temporaire, liste", lambda: depot.list_objects_v2(Bucket=BUCKET), "refusé"),
        tente("dépôt temporaire, effacement", lambda: depot.delete_object(Bucket=BUCKET, Key=cle), "refusé"),
        tente("clé longue des liens, lecture directe", lambda: liens_long.get_object(Bucket=BUCKET, Key=cle), "refusé"),
        tente("liens temporaires, lecture", lambda: liens.get_object(Bucket=BUCKET, Key=cle), "accepté"),
        tente("liens temporaires, écriture", lambda: liens.put_object(Bucket=BUCKET, Key=cle, Body=b"y"), "refusé"),
        tente("liens temporaires, autre utilisateur", lambda: liens.get_object(Bucket=BUCKET, Key="scenes/autre000/essai.blend"), "refusé"),
    ]
    racine().delete_object(Bucket=BUCKET, Key=cle)
    vider()
    if not all(resultats):
        raise SystemExit("recevabilité : échec")
    print("  recevabilité : tout est conforme", flush=True)


if __name__ == "__main__":
    attendre()
    if "--vider" in sys.argv:
        vider()
    elif "--essai" in sys.argv:
        essai()
    else:
        preparer()
