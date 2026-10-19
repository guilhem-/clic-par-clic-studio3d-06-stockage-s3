"""Blender scene storage (module s3d06): another team's S3 service, "storage" namespace.

The studio only speaks S3. Its two long-term keys (upload, links) stay in the MCP server, and serve
one purpose only: getting temporary credentials (STS AssumeRole), limited to the user's folder.
The worker, where the code written by the model runs, only receives those.
"""
import hashlib
import hmac
import json
import re
import time
import unicodedata

import boto3
from botocore.config import Config

import common as c
import metrics as m

SETTINGS = Config(signature_version="s3v4", s3={"addressing_style": "path"},  # path, not subdomain
                  connect_timeout=3, read_timeout=60, retries={"max_attempts": 3, "mode": "standard"},
                  request_checksum_calculation="when_required", response_checksum_validation="when_required")


def _client(service, address, key, secret, token=None):
    return boto3.client(service, endpoint_url=address, region_name=c.S3_REGION, aws_access_key_id=key,
                        aws_secret_access_key=secret, aws_session_token=token, config=SETTINGS)


def pseudonym(email: str) -> str:
    """One folder per user, without their address: the storage team has no need to see it."""
    return hmac.new(c.SECRET, email.encode(), hashlib.sha256).hexdigest()[:16]


def object_key(d: dict) -> str:
    return f"scenes/{pseudonym(d['user'])}/{d['id']}.blend"


def credentials(key: str, secret: str, actions: list, resource: str, use: str) -> dict:
    """Swaps a long-term key for fifteen-minute credentials, limited to `resource`."""
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{c.BUCKET}/{resource}"]}]}
    r = _client("sts", c.S3_INTERNAL, key, secret).assume_role(
        RoleArn="arn:aws:iam::000000000000:role/studio3d", RoleSessionName="studio3d",
        DurationSeconds=c.CREDENTIALS_TTL, Policy=json.dumps(policy))["Credentials"]
    m.CREDENTIALS.labels(use=use).inc()
    return {"key": r["AccessKeyId"], "secret": r["SecretAccessKey"], "token": r["SessionToken"],
            "expires": r["Expiration"].timestamp()}


def prepare_upload(d: dict) -> dict:
    """MCP server: the right to write this one file, in the user's folder."""
    return credentials(c.S3_UPLOAD_KEY, c.S3_UPLOAD_SECRET, ["s3:PutObject"], object_key(d), "upload")


def file_name(d: dict) -> str:
    """Name suggested for the download: the title in ASCII, then the number (living-room-1.blend)."""
    title = unicodedata.normalize("NFKD", d.get("title", "scene")).encode("ascii", "ignore").decode()
    title = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-") or "scene"
    return f"{title}-{d.get('number', '')}.blend"


def upload(path, d: dict, i: dict, creds: dict) -> dict:
    """Worker: uploads the scene with the temporary credentials prepared by the MCP server."""
    size = path.stat().st_size
    if size > c.SCENE_MAX_SIZE:
        raise ValueError("too_large")
    s3 = _client("s3", c.S3_INTERNAL, creds["key"], creds["secret"], creds["token"])
    with path.open("rb") as f:
        rep = s3.put_object(
            Bucket=c.BUCKET, Key=object_key(d), Body=f, ContentLength=size,
            ContentType="application/x-blender",
            ContentDisposition=f'attachment; filename="{file_name(d)}"',
            Metadata={"digital-source-type": "trainedAlgorithmicMedia", "generator": "AI studio3D",
                      "id": f"urn:uuid:{i['id']}", "number": str(d.get("number", "")),
                      "assets": ",".join(a["name"] for a in i.get("assets", []))[:500]})
    return {"key": object_key(d), "version": rep.get("VersionId", ""), "bytes": size}


_read: dict = {}  # read credentials, by pseudonym (kept while they have two minutes left)


def presigned_link(d: dict) -> str:
    """MCP server: sixty-second link, signed for the PUBLIC address (the signature covers the host
    name), with read credentials limited to this user's folder."""
    pseudo = pseudonym(d["user"])
    creds = _read.get(pseudo)
    if not creds or creds["expires"] - time.time() < 120:
        creds = _read[pseudo] = credentials(c.S3_LINKS_KEY, c.S3_LINKS_SECRET,
                                            ["s3:GetObject", "s3:GetObjectVersion"], f"scenes/{pseudo}/*", "read")
    params = {"Bucket": c.BUCKET, "Key": d["scene"]}
    if d.get("scene_version"):
        params["VersionId"] = d["scene_version"]  # the version uploaded by the worker, not another one
    # Signing is a local computation: no network call to the public address.
    return _client("s3", c.S3_PUBLIC, creds["key"], creds["secret"], creds["token"]).generate_presigned_url(
        "get_object", Params=params, ExpiresIn=c.S3_LINK_TTL)
