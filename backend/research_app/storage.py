from pathlib import PurePosixPath


class Storage:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.storage_path.resolve()
        if settings.storage_backend == "s3":
            import boto3

            self.client = boto3.client("s3", region_name=settings.aws_region)
        else:
            self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        parts = PurePosixPath(key)
        if parts.is_absolute() or ".." in parts.parts:
            raise ValueError("Invalid object key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Storage path escapes root")
        return path

    def put(self, key, content, content_type):
        if self.settings.storage_backend == "s3":
            self.client.put_object(
                Bucket=self.settings.s3_bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                ServerSideEncryption="AES256",
            )
        else:
            path = self.path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def get(self, key):
        if self.settings.storage_backend == "s3":
            body = self.client.get_object(Bucket=self.settings.s3_bucket, Key=key)["Body"]
            try:
                return body.read(self.settings.max_upload_bytes + 1)
            finally:
                body.close()
        return self.path(key).read_bytes()

    def delete(self, key):
        if self.settings.storage_backend == "s3":
            self.client.delete_object(Bucket=self.settings.s3_bucket, Key=key)
        else:
            self.path(key).unlink(missing_ok=True)
