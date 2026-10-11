# Télécharger la scène Blender depuis le stockage d'une autre équipe — S3, STS, liens présignés

Code de la démonstration du module 6 de la série « Clic par clic » **Créer un service d'IA
compatible agentique : le studio 3D**.

Vidéo : {{video}}

## But

À la fin de chaque rendu, le studio du module 5 livre aussi **la scène Blender complète**
(`.blend`, textures et éclairage empaquetés), rangée dans le **stockage objet d'une autre équipe** :
un service compatible S3, déployé dans son propre espace de noms (`stockage`). Dans Open WebUI,
l'utilisateur reçoit un lien « Télécharger la scène Blender ».

- **Le studio ne parle que S3.** L'équipe stockage a retenu **RustFS 1.0.1** (Apache 2.0) ;
  MinIO, longtemps la référence, n'est plus distribué (dépôt archivé, images retirées de Docker
  Hub). Le service s'appelle `s3`, pas `rustfs` : le nom du rôle survit au changement de produit.
- **Isolement par utilisateur appliqué par le stockage lui-même.** Un bucket pour le service
  (`studio3d-scenes`), un dossier par utilisateur sous pseudonyme (`scenes/<HMAC de l'adresse>/`).
  Chaque dépôt et chaque lien se font avec des **identifiants temporaires** (STS `AssumeRole`,
  quinze minutes) dont la **politique de session** limite les droits au dossier de l'utilisateur —
  et, pour un dépôt, au seul fichier attendu. Les **clés longue durée** du studio (dépôt, liens) ne
  servent qu'à obtenir ces identifiants : leurs politiques portent la condition
  `"aws:principaltype": "AssumedRole"`, si bien qu'utilisées directement, elles n'écrivent ni ne
  lisent rien.
- **Le worker ne voit jamais de clé.** Le code écrit par le modèle tourne dans le worker ; les clés
  restent dans le serveur MCP, qui prépare les identifiants de dépôt dès qu'un worker prend une
  demande et les lui passe par Redis.
- **Une scène sûre.** Un **second Blender, neuf**, ouvre la scène sans exécuter ses scripts
  (`--disable-autoexec`), retire tout texte et toute expression Python laissés par le code du
  modèle, inscrit le marquage du module 4, empaquette textures et HDRI (`pack_all`), enregistre un
  fichier compressé.
- **Un clic, deux signatures.** Le lien du studio (`/r/<n>/<signature>.blend`, stable) répond par
  une **redirection 302** vers un **lien présigné de soixante secondes**, signé pour l'**adresse
  publique** du stockage (une signature S3 couvre le nom d'hôte) et lié à la **version** déposée
  (bucket versionné).
- **Un service tiers ne fait pas tomber le nôtre.** Stockage absent ou refus : le rendu reste
  réussi (image, modèle 3D), la scène est notée indisponible.
- **En coulisses :** cycle de vie du bucket (scènes effacées au bout de 7 jours, versions
  remplacées au bout d'un jour, envois inachevés au bout d'un jour) ; mesures de RustFS (poussées en
  OTLP vers un collecteur OpenTelemetry) et du studio dans une rangée « Stockage des scènes » du
  tableau Grafana du module 3.

## Pourquoi pas un bucket par utilisateur ?

Un bucket est une ressource d'administration : il faudrait un administrateur du stockage à chaque
arrivée, et autant de politiques. Les dossiers (préfixes) se créent tout seuls au premier dépôt, et
la politique de session, calculée à chaque demande, suffit à isoler les utilisateurs.

## Contenu

| Fichier | Rôle |
|---|---|
| `stockage/k8s.yaml` | Espace `stockage` : volume persistant, RustFS 1.0.1 (image épinglée par empreinte), service `s3`, règle d'entrée `s3-entrees` (studio, Traefik, préparation), `IngressRoute` publique limitée à `GET` et `HEAD` (`s3.localhost`), console (`stockage.localhost`, pour la démonstration), collecteur OpenTelemetry |
| `stockage/politiques/*.json` | Politiques IAM : `depot` (`s3:PutObject` sur `scenes/*`), `liens` (`s3:GetObject`, `s3:GetObjectVersion`), toutes deux réservées aux identifiants temporaires ; `consultation` (liste et lecture, pour la console) |
| `stockage/cycle-de-vie.json` | Règles de cycle de vie du bucket |
| `stockage/preparation/` | Image de préparation (Python, boto3, client `rc` de RustFS) et `preparer.py` : bucket, versionnement, cycle de vie, politiques, comptes ; `--vider`, `--essai` (tests de recevabilité) |
| `stockage/preparation.yaml`, `stockage/collecteur.yaml`, `stockage/deployer.sh` | Job de préparation, configuration du collecteur, script de déploiement |
| `studio/stockage.py` | Pseudonyme, identifiants temporaires (`AssumeRole` + politique de session), dépôt, lien présigné |
| `studio/empaqueter.py` | Le second Blender : nettoyage, marquage, empaquetage |
| `studio/api.py`, `travailleur.py`, `rendu.py`, `commun.py`, `metriques.py` | Préparation des identifiants de dépôt, route `/r/…blend`, champ `scene_blend_markdown`, enregistrement et dépôt de la scène, mesures |
| `studio/lire_scene.py` | Lecture d'une scène téléchargée (objets, images empaquetées, scripts, marquage) |
| `studio/Dockerfile` | Image `studio3d:5` |
| `k8s-echelle.yaml` | Clés du secret `stockage` (facultatif) au seul serveur MCP ; adresse du stockage aux workers |
| `k8s-stockage.yaml` | Règle de sortie du studio vers le service `s3` de l'espace `stockage` |
| `observabilite/` | Tâche Prometheus `stockage` et tableau Grafana avec la rangée « Stockage des scènes » |
| `essai/` | `piege-signature.py` (le piège du nom d'hôte), scènes d'essai (salon, scène hostile) |
| `open-webui/` | En-tête d'identité et prompt système du modèle « Studio 3D » |

## Prérequis

- Le studio du **module 5** (dépôt
  [`clic-par-clic-studio3d-05-assets`](https://github.com/guilhem-/clic-par-clic-studio3d-05-assets)),
  et l'observabilité du module 3 pour la rangée Grafana.
- Cluster **k3d** (`tterm` dans la vidéo, k3s 1.35, Traefik 3 publié sur le port 8080), `kubectl`,
  Docker, `openssl`.

## Installation pas à pas

1. **Le stockage** (équipe stockage) :

   ```bash
   ./stockage/deployer.sh            # RustFS, collecteur, bucket, cycle de vie, comptes
   ./stockage/deployer.sh --essai    # recevabilité : refus attendus, identifiants temporaires acceptés
   ./stockage/deployer.sh --cles     # remise des clés de dépôt et des liens au studio (secret « stockage »)
   ```

   Les secrets sont créés dans `stockage/.env` (hors git ; modèle : `.env.example`). En production,
   la remise des clés passe par le coffre de l'organisation.

2. **Le studio** :

   ```bash
   docker build -t studio3d:5 studio && k3d image import studio3d:5 -c tterm
   kubectl apply -f k8s-echelle.yaml -f k8s-stockage.yaml
   kubectl -n studio3d rollout restart deployment/studio-api
   ```

3. **Observabilité** (facultatif) : reprenez `observabilite/prometheus.yml` et
   `observabilite/tableaux/studio3d.json` dans le déploiement du module 3.

4. **Open WebUI** : remplacez le prompt système du modèle « Studio 3D » par
   `open-webui/consignes-modele.txt` (la réponse d'état recopie le lien de la scène).

5. **Essai** : « Crée une scène de salon avec le fauteuil et le vase en céramique de la
   bibliothèque, sous l'éclairage salon bois. », puis « Où en est mon rendu ? », puis cliquez sur
   « Télécharger la scène Blender ». Lecture du fichier, sans réseau ni bibliothèque :

   ```bash
   docker run --rm --network none -v "$PWD:/scene:ro" studio3d:5 /opt/blender/blender -b \
     --factory-startup --disable-autoexec /scene/salon-1.blend --python /app/lire_scene.py
   ```

6. **Le piège du nom d'hôte** (clé et version de l'objet lues dans Redis) :

   ```bash
   kubectl exec -i -n studio3d deploy/studio-api -- python - <clé> <version> < essai/piege-signature.py
   ```

   Le lien signé pour l'adresse interne ne se résout pas depuis le poste ; remplacer l'adresse à la
   main donne `SignatureDoesNotMatch` ; le lien signé pour `http://s3.localhost:8080` fonctionne.
   Utilisez `GET` (pas `curl -I`) : la méthode fait partie de la signature.

## Points à connaître

- **Clé de condition** : RustFS attend `aws:principaltype` en minuscules (la forme `aws:PrincipalType`
  est refusée à la création de la politique).
- **Durée** : STS impose quinze minutes au minimum ; le lien présigné, lui, vit soixante secondes.
- **Le lien du studio est un lien porteur**, comme ceux des images : quiconque l'a reçu peut
  télécharger tant que la scène existe (7 jours). L'identité est contrôlée quand le lien est donné
  (`etat_rendu`).
- **Limite assumée** : le code du modèle s'exécute dans le worker, qui a accès à Redis (module 2).
  Il pourrait y lire les identifiants de dépôt d'une autre demande en cours — quinze minutes,
  écriture d'un seul fichier — mais jamais une clé longue durée, ni le droit de lire une scène ; et
  le lien livré pointe la version déposée par le worker.
- **Console** : publiée pour la démonstration, avec un compte de consultation ; en production, elle
  reste interne.
- **Compatibilité** : fichier enregistré par Blender 5.2, à ouvrir avec Blender 5.x.
- **Taille maximale** : 200 Mo par scène (`SCENE_TAILLE_MAX`).

## Sources

- RustFS : [installation en conteneur](https://docs.rustfs.com/installation/container),
  [politiques IAM](https://docs.rustfs.com/administration/iam/policies),
  [comptes de service et STS](https://docs.rustfs.com/administration/iam/sts),
  [observabilité (OTLP)](https://docs.rustfs.com/operations/observability),
  [variables d'environnement](https://docs.rustfs.com/reference/environment-variables),
  [client `rc`](https://github.com/rustfs/cli).
- AWS : [cycle de vie S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html),
  [`AssumeRole` et politiques de session](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html),
  [URL présignées](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html),
  [Signature V4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html).
- MinIO : [dépôt archivé](https://github.com/minio/minio).
- [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/),
  [Prometheus : configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
  [Grafana : provisionnement](https://grafana.com/docs/grafana/latest/administration/provisioning/).
- Blender : [scripts automatiques et sécurité](https://docs.blender.org/manual/en/latest/advanced/scripting/security.html).

## Licence

Code : MIT, voir [`LICENSE`](LICENSE). RustFS, MinIO, Blender, Redis, KEDA, OpenTelemetry,
Prometheus et Grafana sont des projets indépendants ; cette série est indépendante des éditeurs des
logiciels présentés.

## Suite de la série

- Module précédent : **Une bibliothèque d'assets pour tous les rendus** — dépôt
  [`clic-par-clic-studio3d-05-assets`](https://github.com/guilhem-/clic-par-clic-studio3d-05-assets).
- À suivre : la série **Intégrations IA**, qui relie ce studio à Open WebUI de quatre façons.
