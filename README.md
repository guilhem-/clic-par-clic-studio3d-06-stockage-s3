# Downloading the Blender scene from another team's storage — S3, STS, presigned links

Demo code for module 6 of the "Click by Click" series **Building an agent-ready AI service: the 3D
studio**.

Video: {{video}}

## Goal

At the end of each render, the module 5 studio also delivers **the complete Blender scene**
(`.blend`, textures and lighting packed), stored in **another team's object storage**: an
S3-compatible service, deployed in its own namespace (`storage`). In Open WebUI, the user gets a
"Download the Blender scene" link.

- **The studio only speaks S3.** The storage team chose **RustFS 1.0.1** (Apache 2.0); MinIO, long
  the reference, is no longer distributed (archived repository, images removed from Docker Hub).
  The service is called `s3`, not `rustfs`: the role's name outlives a product change.
- **User isolation enforced by the storage itself.** One bucket for the service
  (`studio3d-scenes`), one folder per user under a pseudonym (`scenes/<HMAC of the address>/`).
  Every upload and every link uses **temporary credentials** (STS `AssumeRole`, fifteen minutes)
  whose **session policy** limits the rights to the user's folder — and, for an upload, to the one
  expected file. The studio's **long-term keys** (upload, links) only serve to obtain these
  credentials: their policies carry the condition `"aws:principaltype": "AssumedRole"`, so used
  directly, they write and read nothing.
- **The worker never sees a key.** The code written by the model runs in the worker; the keys stay
  in the MCP server, which prepares the upload credentials as soon as a worker takes a request and
  hands them over through Redis.
- **A safe scene.** A **second, fresh Blender** opens the scene without running its scripts
  (`--disable-autoexec`), removes every text block and Python expression left by the model's code,
  writes the module 4 marking, packs textures and HDRI (`pack_all`), and saves a compressed file.
- **One click, two signatures.** The studio link (`/r/<n>/<signature>.blend`, stable) answers with
  a **302 redirect** to a **sixty-second presigned link**, signed for the storage's **public
  address** (an S3 signature covers the host name) and tied to the uploaded **version** (versioned
  bucket).
- **A third-party service does not bring ours down.** Storage missing or refusing: the render still
  succeeds (image, 3D model), the scene is marked unavailable.
- **Behind the scenes:** bucket lifecycle (scenes deleted after 7 days, replaced versions after one
  day, incomplete uploads after one day); metrics from RustFS (pushed in OTLP to an OpenTelemetry
  collector) and from the studio in a "Scene storage" row of the module 3 Grafana dashboard.

## Why not one bucket per user?

A bucket is an administration resource: every new arrival would need a storage administrator, and
as many policies. Folders (prefixes) appear by themselves on the first upload, and the session
policy, computed for each request, is enough to isolate users.

## Contents

| File | Role |
|---|---|
| `storage/k8s.yaml` | `storage` namespace: persistent volume, RustFS 1.0.1 (image pinned by digest), `s3` service, `s3-ingress` rule (studio, Traefik, prepare Job), public `IngressRoute` limited to `GET` and `HEAD` (`s3.localhost`), console (`stockage.localhost`, for the demo), OpenTelemetry collector |
| `storage/policies/*.json` | IAM policies: `upload` (`s3:PutObject` on `scenes/*`), `links` (`s3:GetObject`, `s3:GetObjectVersion`), both reserved for temporary credentials; `viewer` (list and read, for the console) |
| `storage/lifecycle.json` | Bucket lifecycle rules |
| `storage/prepare/` | Preparation image (Python, boto3, RustFS `rc` client) and `prepare.py`: bucket, versioning, lifecycle, policies, accounts; `--empty`, `--test` (acceptance tests) |
| `storage/prepare.yaml`, `storage/collector.yaml`, `storage/deploy.sh` | Prepare Job, collector configuration, deployment script |
| `studio/storage.py` | Pseudonym, temporary credentials (`AssumeRole` + session policy), upload, presigned link |
| `studio/package.py` | The second Blender: cleaning, marking, packing |
| `studio/api.py`, `worker.py`, `render.py`, `common.py`, `metrics.py` | Upload credentials preparation, `/r/…blend` route, `scene_blend_markdown` field, saving and uploading the scene, metrics |
| `studio/read_scene.py` | Reads a downloaded scene (objects, packed images, scripts, marking) |
| `studio/Dockerfile` | Image `studio3d:5` |
| `k8s-scale.yaml` | Keys of the `storage` secret (optional) to the MCP server only; storage address to the workers |
| `k8s-storage.yaml` | Egress rule from the studio to the `s3` service of the `storage` namespace |
| `observability/` | Prometheus `storage` job and Grafana dashboard with the "Scene storage" row (`dashboards/studio3d.json`; French and German titles: `.fr.json`, `.de.json`) |
| `test/` | `signature-trap.py` (the hostname trap), test scenes (living room, hostile scene) |
| `open-webui/` | Identity header and system prompt of the "Studio 3D" model |

## Prerequisites

- The **module 5** studio (repository
  [`clic-par-clic-studio3d-05-assets`](https://github.com/guilhem-/clic-par-clic-studio3d-05-assets)),
  and the module 3 observability for the Grafana row.
- A **k3d** cluster (`tterm` in the video, k3s 1.35, Traefik 3 published on port 8080), `kubectl`,
  Docker, `openssl`.

## Step-by-step installation

1. **The storage** (storage team):

   ```bash
   ./storage/deploy.sh            # RustFS, collector, bucket, lifecycle, accounts
   ./storage/deploy.sh --test     # acceptance: expected refusals, temporary credentials accepted
   ./storage/deploy.sh --keys     # hands the upload and links keys to the studio ("storage" secret)
   ```

   The secrets are created in `storage/.env` (outside git; template: `.env.example`). In
   production, the keys are handed over through the organization's vault.

2. **The studio**:

   ```bash
   docker build -t studio3d:5 studio && k3d image import studio3d:5 -c tterm
   kubectl apply -f k8s-scale.yaml -f k8s-storage.yaml
   kubectl -n studio3d rollout restart deployment/studio-api
   ```

   The labels returned to the model ("Download the Blender scene") follow `STUDIO_LANGUAGE`
   (`EN` by default, `FR`, `DE`), an optional environment variable of `studio-api`.

3. **Observability** (optional): reuse `observability/prometheus.yml` and
   `observability/dashboards/studio3d.json` in the module 3 deployment.

4. **Open WebUI**: replace the system prompt of the "Studio 3D" model with
   `open-webui/system-prompt.txt` (the status answer copies the scene link).

5. **Test**: "Create a living room scene with the library's armchair and ceramic vase, under the
   wooden lounge lighting.", then "How is my render doing?", then click "Download the Blender
   scene". Reading the file, without network or library:

   ```bash
   docker run --rm --network none -v "$PWD:/scene:ro" studio3d:5 /opt/blender/blender -b \
     --factory-startup --disable-autoexec /scene/living-room-1.blend --python /app/read_scene.py
   ```

6. **The hostname trap** (object key and version read in Redis):

   ```bash
   kubectl exec -i -n studio3d deploy/studio-api -- python - <key> <version> < test/signature-trap.py
   ```

   The link signed for the internal address does not resolve from the workstation; replacing the
   address by hand gives `SignatureDoesNotMatch`; the link signed for `http://s3.localhost:8080`
   works. Use `GET` (not `curl -I`): the method is part of the signature.

## Good to know

- **Condition key**: RustFS expects `aws:principaltype` in lower case (the `aws:PrincipalType`
  form is refused when the policy is created).
- **Duration**: STS enforces fifteen minutes at least; the presigned link lives sixty seconds.
- **The studio link is a bearer link**, like the image links: anyone who received it can download
  while the scene exists (7 days). Identity is checked when the link is given (`render_status`).
- **Known limit**: the model's code runs in the worker, which has access to Redis (module 2). It
  could read there the upload credentials of another running request — fifteen minutes, writing a
  single file — but never a long-term key, nor the right to read a scene; and the delivered link
  points to the version uploaded by the worker. Module 7 removes this limit.
- **Console**: published for the demo, with a viewer account; in production, it stays internal.
- **Compatibility**: file saved by Blender 5.2, to open with Blender 5.x.
- **Maximum size**: 200 MB per scene (`SCENE_MAX_SIZE`).

## Sources

- RustFS: [container installation](https://docs.rustfs.com/installation/container),
  [IAM policies](https://docs.rustfs.com/administration/iam/policies),
  [service accounts and STS](https://docs.rustfs.com/administration/iam/sts),
  [observability (OTLP)](https://docs.rustfs.com/operations/observability),
  [environment variables](https://docs.rustfs.com/reference/environment-variables),
  [`rc` client](https://github.com/rustfs/cli).
- AWS: [S3 lifecycle](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html),
  [`AssumeRole` and session policies](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html),
  [presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html),
  [Signature V4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html).
- MinIO: [archived repository](https://github.com/minio/minio).
- [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/),
  [Prometheus: configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
  [Grafana: provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/).
- Blender: [auto-run scripts and security](https://docs.blender.org/manual/en/latest/advanced/scripting/security.html).

## License

Code: MIT, see [`LICENSE`](LICENSE). RustFS, MinIO, Blender, Redis, KEDA, OpenTelemetry,
Prometheus and Grafana are independent projects; this series is independent of the publishers of
the software shown.

## Rest of the series

- Previous module: **An asset library for every render** — repository
  [`clic-par-clic-studio3d-05-assets`](https://github.com/guilhem-/clic-par-clic-studio3d-05-assets).
- Next module: **A personal storage space: carrying the identity all the way to storage** —
  repository [`clic-par-clic-studio3d-07-identite-stockage`](https://github.com/guilhem-/clic-par-clic-studio3d-07-identite-stockage).
- To connect this studio to Open WebUI in four different ways: the **AI Integrations** series.
