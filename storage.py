import os, uuid, boto3
from botocore.client import Config

_ENDPOINT = f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
_BUCKET   = os.getenv("R2_BUCKET")

_session = boto3.session.Session()
_client  = _session.client(
    "s3",
    region_name="auto",                               # ← AGGIUNGI QUESTO
    endpoint_url=_ENDPOINT,
    aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
    aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
    config=Config(signature_version="s3v4"),
)

def new_file_key(original_name: str) -> str:
    ext = original_name.rsplit(".", 1)[-1].lower()
    return f"{uuid.uuid4()}.{ext}"

def presign_put(key: str, expires=900) -> str:
    return _client.generate_presigned_url(
        "put_object",
        Params={"Bucket": _BUCKET, "Key": key},
        ExpiresIn=expires,
        HttpMethod="PUT",
    )
