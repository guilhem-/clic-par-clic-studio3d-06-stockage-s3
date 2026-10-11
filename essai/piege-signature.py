"""Le piège du nom d'hôte (module s3d06), dans un pod du serveur MCP : le même objet, signé deux fois.

    kubectl exec -i -n studio3d deploy/studio-api -- python - <clé de l'objet> <version> < piege-signature.py

Imprime deux liens présignés de soixante secondes pour la même scène : l'un pour l'adresse interne
du stockage, l'autre pour son adresse publique. Les identifiants de lecture restent dans le pod ;
seuls les liens sortent.
"""
import sys

sys.path.insert(0, "/app")
import commun as c  # noqa: E402
import stockage  # noqa: E402

cle, version = sys.argv[1], sys.argv[2]
dossier = cle.rsplit("/", 1)[0]  # scenes/<pseudonyme>
ident = stockage.identifiants(c.S3_LIENS_CLE, c.S3_LIENS_SECRET, ["s3:GetObject", "s3:GetObjectVersion"],
                              f"{dossier}/*", "lecture")
for adresse in (c.S3_INTERNE, c.S3_PUBLIC):  # même objet, mêmes identifiants, deux adresses
    s3 = stockage._client("s3", adresse, ident["cle"], ident["secret"], ident["jeton"])
    print(s3.generate_presigned_url("get_object", Params={"Bucket": c.BUCKET, "Key": cle, "VersionId": version},
                                    ExpiresIn=c.DUREE_LIEN_S3))
