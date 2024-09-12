# third party
from sqlalchemy import URL

# relative
from ...serde.serializable import serializable
from ...server.credentials import SyftVerifyKey
from ...types.uid import UID
from .base import DBManager
from .schema import Base
from .sqlite_db import DBConfig


@serializable(canonical_name="PostgresDBConfig", version=1)
class PostgresDBConfig(DBConfig):
    host: str
    port: int
    user: str
    password: str
    database: str

    @property
    def connection_string(self) -> str:
        return URL.create(
            "postgresql",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        ).render_as_string(hide_password=False)


class PostgresDBManager(DBManager):
    def update_settings(self) -> None:
        return super().update_settings()

    def init_tables(self) -> None:
        if self.config.reset:
            # drop all tables that we know about
            Base.metadata.drop_all(bind=self.engine)
            self.config.reset = False

        Base.metadata.create_all(self.engine)

    def reset(self) -> None:
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(self.engine)

    @classmethod
    def random(
        cls: type,
        *,
        config: PostgresDBConfig,
        server_uid: UID | None = None,
        root_verify_key: SyftVerifyKey | None = None,
    ) -> "PostgresDBManager":
        root_verify_key = root_verify_key or SyftVerifyKey.generate()
        server_uid = server_uid or UID()
        return PostgresDBManager(
            config=config, server_uid=server_uid, root_verify_key=root_verify_key
        )
