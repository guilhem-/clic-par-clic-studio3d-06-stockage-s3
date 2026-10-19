"""The hostname trap (module s3d06), in an MCP server pod: the same object, signed twice.

    kubectl exec -i -n studio3d deploy/studio-api -- python - <object key> <version> < signature-trap.py

Prints two sixty-second presigned links for the same scene: one for the internal address of the
storage, the other for its public address. The read credentials stay in the pod; only the links
come out.
"""
import sys

sys.path.insert(0, "/app")
import common as c  # noqa: E402
import storage  # noqa: E402

key, version = sys.argv[1], sys.argv[2]
folder = key.rsplit("/", 1)[0]  # scenes/<pseudonym>
creds = storage.credentials(c.S3_LINKS_KEY, c.S3_LINKS_SECRET, ["s3:GetObject", "s3:GetObjectVersion"],
                            f"{folder}/*", "read")
for address in (c.S3_INTERNAL, c.S3_PUBLIC):  # same object, same credentials, two addresses
    s3 = storage._client("s3", address, creds["key"], creds["secret"], creds["token"])
    print(s3.generate_presigned_url("get_object", Params={"Bucket": c.BUCKET, "Key": key, "VersionId": version},
                                    ExpiresIn=c.S3_LINK_TTL))
