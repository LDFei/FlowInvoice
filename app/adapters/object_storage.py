# app/adapters/object_storage.py —— 对象存储双实现（ObjectStorage 接口）
# 业务：发票源文件不落 DB、落对象存储（MinIO/S3），DB 只存 file_key（docs/06 Phase 2）。
#       生产走 MinIO（惰性导入：未配置时零依赖）；本地/测试走目录实现（SqliteStorage 同角色）。
from pathlib import Path

from app.adapters.base import ObjectStorage
from app.core.config import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    MINIO_SECURE,
    OBJECT_DIR,
)


class LocalObjectStorage(ObjectStorage):
    """本地目录对象存储（测试替身 / 无 MinIO 环境；路径即 key）"""

    def __init__(self, root: Path):
        self._root = Path(root)

    def put(self, key: str, content: bytes) -> str:
        p = self._root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return key

    def get(self, key: str) -> bytes:
        return (self._root / key).read_bytes()

    def exists(self, key: str) -> bool:
        return (self._root / key).is_file()

    def delete(self, key: str) -> None:
        (self._root / key).unlink(missing_ok=True)


class MinioObjectStorage(ObjectStorage):
    """MinIO / S3 对象存储（生产；惰性导入，未配置时零依赖——与 PgStorage 惰性导入同范式）"""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ):
        # 作用：minio 包只在显式配置后引入（避免基础安装拖入无关依赖）
        from minio import Minio
        from minio.error import S3Error

        self._S3Error = S3Error
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        # 作用：启动即确认桶存在——桶缺失所有读写都会挂，fail-fast 与 pg_storage 启动即失败一致
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)

    def put(self, key: str, content: bytes) -> str:
        from io import BytesIO

        self._client.put_object(self._bucket, key, BytesIO(content), length=len(content))
        return key

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except self._S3Error:
            return False

    def delete(self, key: str) -> None:
        # 作用：remove_object 幂等——对象不存在也不报错（与 Local 行为对齐）
        self._client.remove_object(self._bucket, key)


def build_object_storage(local_root: Path | None = None) -> ObjectStorage:
    """按配置装配：设置 FLOWINVOICE_MINIO_ENDPOINT → MinIO 生产后端；否则本地目录（测试/离线替身）
    local_root：显式指定本地替身根目录（测试传 tmp 隔离；缺省全局 OBJECT_DIR）"""
    if MINIO_ENDPOINT:
        return MinioObjectStorage(
            endpoint=MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            bucket=MINIO_BUCKET,
            secure=MINIO_SECURE,
        )
    return LocalObjectStorage(local_root or OBJECT_DIR)
