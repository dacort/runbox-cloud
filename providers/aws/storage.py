from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .base import AWSResource
from .decorators import depends_on
from .network import VPC


@depends_on(VPC)
class S3Bucket(AWSResource):
    resource_type = "storage"

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        region: str = "us-east-1",
        name: Optional[str] = None,
        **kwargs,
    ):
        self.bucket_name = bucket_name
        self.region = region
        super().__init__(name=name, **kwargs)

    def _create(self) -> str:
        s3 = boto3.client("s3", region_name=self.region)

        if not self.bucket_name:
            import uuid

            self.bucket_name = f"mybucket-{uuid.uuid4().hex[:8]}"

        if self.region != "us-east-1":
            s3.create_bucket(
                Bucket=self.bucket_name,
                CreateBucketConfiguration={"LocationConstraint": self.region},
            )
        else:
            s3.create_bucket(Bucket=self.bucket_name)

        # Update config
        self._config.update({"bucket_name": self.bucket_name, "region": self.region})

        return self.bucket_name

    def _destroy(self):
        if not self._config.get("bucket_name"):
            return

        s3 = boto3.client("s3", region_name=self._config["region"])

        # Delete all objects first
        try:
            response = s3.list_objects_v2(Bucket=self._config["bucket_name"])
            if "Contents" in response:
                objects = [{"Key": obj["Key"]} for obj in response["Contents"]]
                s3.delete_objects(
                    Bucket=self._config["bucket_name"], Delete={"Objects": objects}
                )
        except ClientError:
            pass

        # Delete bucket
        s3.delete_bucket(Bucket=self._config["bucket_name"])

    def _exists(self) -> bool:
        if not self._config.get("bucket_name"):
            return False

        s3 = boto3.client("s3", region_name=self._config["region"])
        try:
            s3.head_bucket(Bucket=self._config["bucket_name"])
            return True
        except ClientError:
            return False