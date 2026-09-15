"""実PostgreSQLに対するmigration制約テスト。"""

import os
import subprocess
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="AIRPG_TEST_DATABASE_URLが未設定です")


@pytest.fixture()
def database() -> Iterator[Engine]:
    assert URL
    env = {**os.environ, "AIRPG_DATABASE_URL": URL}
    subprocess.run(["alembic", "-x", f"url={URL}", "upgrade", "head"], check=True, env=env)
    engine = create_engine(URL)
    yield engine
    subprocess.run(["alembic", "-x", f"url={URL}", "downgrade", "base"], check=True, env=env)


def test_upgrade_constraints_append_only_and_rollback(database: Engine) -> None:
    """複合FK、部分一意、追記禁止とtransaction rollbackをまとめて確認する。"""
    with database.begin() as c:
        c.execute(
            text(
                "INSERT INTO campaigns(id,ruleset_version) VALUES('00000000-0000-0000-0000-000000000001','mvp_v1'),('00000000-0000-0000-0000-000000000002','mvp_v1')"
            )
        )
        c.execute(
            text(
                "INSERT INTO scenes(id,campaign_id,sequence,status) VALUES('00000000-0000-0000-0000-000000000011','00000000-0000-0000-0000-000000000001',1,'active')"
            )
        )
        with pytest.raises(Exception):
            c.execute(
                text(
                    "INSERT INTO scenes(id,campaign_id,sequence,status) VALUES('00000000-0000-0000-0000-000000000012','00000000-0000-0000-0000-000000000001',2,'active')"
                )
            )
    with database.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM campaigns")) == 0
