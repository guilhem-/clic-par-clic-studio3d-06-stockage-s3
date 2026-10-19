"""S3 storage preparation by the storage team (module s3d06). Idempotent: safe to run again.

  python prepare.py            bucket, versioning, lifecycle, policies, accounts, final check
  python prepare.py --empty    empties the scenes/ folder (all versions)
  python prepare.py --test     acceptance tests (temporary credentials, expected refusals)

Run by a Kubernetes Job in the "storage" namespace, with the root key (s3-root secret) and the
account keys (accounts secret). Files read in /config: policies/*.json, lifecycle.json.
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

S3 = os.environ.get("S3_INTERNAL", "http://s3.storage.svc.cluster.local:9000")
BUCKET = os.environ.get("BUCKET", "studio3d-scenes")
CONFIG = Path(os.environ.get("CONFIG", "/config"))
SETTINGS = Config(signature_version="s3v4", s3={"addressing_style": "path"},
                  request_checksum_calculation="when_required", response_checksum_validation="when_required")
# Three accounts, three policies; the root key is never handed to the studio.
ACCOUNTS = {"studio3d-upload": "UPLOAD", "studio3d-links": "LINKS", "studio3d-viewer": "VIEWER"}


def client(key, secret, token=None, service="s3"):
    return boto3.client(service, endpoint_url=S3, region_name="us-east-1", aws_access_key_id=key,
                        aws_secret_access_key=secret, aws_session_token=token, config=SETTINGS)


def root():
    return client(os.environ["ROOT_KEY"], os.environ["ROOT_SECRET"])


def rc(*args):
    """RustFS rc client (accounts and policies); the output is summed up in one line."""
    p = subprocess.run(["rc", "--no-color", *args], capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip().splitlines()
    return p.returncode, (out[-1] if out else "")


def step(text):
    print(f"  {text}", flush=True)


def wait():
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{S3}/health", timeout=2):
                return
        except Exception:
            time.sleep(2)
    raise SystemExit("the s3 service does not answer")


def prepare():
    s3 = root()
    try:
        s3.head_bucket(Bucket=BUCKET)
        step(f"bucket {BUCKET}: already there")
    except ClientError:
        s3.create_bucket(Bucket=BUCKET)
        step(f"bucket {BUCKET}: created")
    # Versioning: a replaced object keeps its original version (the studio ties each link to its own).
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    step("versioning: enabled")
    # Lifecycle: scenes deleted after 7 days, incomplete uploads after one day.
    rules = json.loads((CONFIG / "lifecycle.json").read_text(encoding="utf-8"))
    s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration=rules)
    step("lifecycle: " + ", ".join(r["ID"] for r in rules["Rules"]))

    # Accounts and policies, with the root key ("root" alias of the rc client).
    rc("alias", "set", "root", S3, os.environ["ROOT_KEY"], os.environ["ROOT_SECRET"])
    for account, prefix in ACCOUNTS.items():
        policy = CONFIG / "policies" / f"{account.removeprefix('studio3d-')}.json"
        code, msg = rc("admin", "policy", "create", "root", account, str(policy))
        assert code == 0, msg
        code, msg = rc("admin", "user", "add", "root", account, os.environ[f"{prefix}_SECRET"])
        assert code == 0, msg
        code, msg = rc("admin", "policy", "attach", "root", account, "--user", account)
        assert code == 0 or "already" in msg.lower(), msg
        step(f"account {account}: policy {account}")

    # Final check: what is really in place.
    reread = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    step(f"check: {len(reread)} lifecycle rules, versioning "
         f"{s3.get_bucket_versioning(Bucket=BUCKET).get('Status')}")


def empty():
    s3 = root()
    n = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=BUCKET, Prefix="scenes/"):
        objects = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        if objects:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objects, "Quiet": True})
            n += len(objects)
    step(f"scenes/ emptied: {n} versions deleted")


def test():
    """Acceptance: the long-term keys can only obtain temporary credentials."""
    def attempt(name, f, expected):
        try:
            f()
            got = "accepted"
        except ClientError as e:
            got = "refused" if e.response["Error"]["Code"] in ("AccessDenied", "403") else e.response["Error"]["Code"]
        print(f"  {'ok ' if got == expected else 'KO '} {name}: {got}", flush=True)
        return got == expected

    def temporary(account, actions, resource):
        sts = client(account, os.environ[f"{ACCOUNTS[account]}_SECRET"], service="sts")
        policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": actions, "Resource": [resource]}]}
        c = sts.assume_role(RoleArn="arn:aws:iam::000000000000:role/studio3d", RoleSessionName="test",
                            DurationSeconds=900, Policy=json.dumps(policy))["Credentials"]
        return client(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])

    key = "scenes/test0000/test.blend"
    upload_long = client("studio3d-upload", os.environ["UPLOAD_SECRET"])
    links_long = client("studio3d-links", os.environ["LINKS_SECRET"])
    upload = temporary("studio3d-upload", ["s3:PutObject"], f"arn:aws:s3:::{BUCKET}/{key}")
    links = temporary("studio3d-links", ["s3:GetObject", "s3:GetObjectVersion"], f"arn:aws:s3:::{BUCKET}/scenes/test0000/*")
    results = [
        attempt("long-term upload key, direct write", lambda: upload_long.put_object(Bucket=BUCKET, Key=key, Body=b"x"), "refused"),
        attempt("temporary upload, the planned file", lambda: upload.put_object(Bucket=BUCKET, Key=key, Body=b"x"), "accepted"),
        attempt("temporary upload, another file", lambda: upload.put_object(Bucket=BUCKET, Key="scenes/test0000/other.blend", Body=b"x"), "refused"),
        attempt("temporary upload, another user", lambda: upload.put_object(Bucket=BUCKET, Key="scenes/other000/test.blend", Body=b"x"), "refused"),
        attempt("temporary upload, read", lambda: upload.get_object(Bucket=BUCKET, Key=key), "refused"),
        attempt("temporary upload, list", lambda: upload.list_objects_v2(Bucket=BUCKET), "refused"),
        attempt("temporary upload, delete", lambda: upload.delete_object(Bucket=BUCKET, Key=key), "refused"),
        attempt("long-term links key, direct read", lambda: links_long.get_object(Bucket=BUCKET, Key=key), "refused"),
        attempt("temporary links, read", lambda: links.get_object(Bucket=BUCKET, Key=key), "accepted"),
        attempt("temporary links, write", lambda: links.put_object(Bucket=BUCKET, Key=key, Body=b"y"), "refused"),
        attempt("temporary links, another user", lambda: links.get_object(Bucket=BUCKET, Key="scenes/other000/test.blend"), "refused"),
    ]
    root().delete_object(Bucket=BUCKET, Key=key)
    empty()
    if not all(results):
        raise SystemExit("acceptance: failed")
    print("  acceptance: everything conforms", flush=True)


if __name__ == "__main__":
    wait()
    if "--empty" in sys.argv:
        empty()
    elif "--test" in sys.argv:
        test()
    else:
        prepare()
