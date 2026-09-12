import boto3
from botocore.config import Config

from app.config import settings


def get_s3_client():
    if settings.s3_endpoint_url is None:
        raise RuntimeError("S3_ENDPOINT_URL is not configured")

    if settings.s3_access_key_id is None:
        raise RuntimeError("S3_ACCESS_KEY_ID is not configured")

    if settings.s3_secret_access_key is None:
        raise RuntimeError("S3_SECRET_ACCESS_KEY is not configured")

    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        region_name=settings.s3_region,
        config=Config(
            s3={"addressing_style": "path"},
        ),
    )


def upload_object(key: str, data: bytes, content_type: str) -> None:
    if settings.s3_bucket is None:
        raise RuntimeError("S3_BUCKET is not configured")

    client = get_s3_client()

    client.put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


def get_object(key: str) -> bytes:
    if settings.s3_bucket is None:
        raise RuntimeError("S3_BUCKET is not configured")

    client = get_s3_client()

    response = client.get_object(
        Bucket=settings.s3_bucket,
        Key=key,
    )

    return response["Body"].read()


def delete_object(key: str) -> None:
    if settings.s3_bucket is None:
        raise RuntimeError("S3_BUCKET is not configured")

    client = get_s3_client()

    client.delete_object(
        Bucket=settings.s3_bucket,
        Key=key,
    )
