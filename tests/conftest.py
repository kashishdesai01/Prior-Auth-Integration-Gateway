import time

import psycopg
import pytest
from fastapi.testclient import TestClient
from jwt import PyJWK
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pagw.api import create_app
from pagw.config import Settings
from pagw.db import Database
from pagw.mocks import mint
from pagw.queueing import PostgresBroker
from pagw.security import public_jwk
from pagw.store import Store


@pytest.fixture(scope="session")
def settings():
    base = Settings()
    parts = conninfo_to_dict(base.database_url.get_secret_value())
    parts["dbname"] = "pagw_test"
    dsn = make_conninfo(**parts)
    admin = dict(parts, dbname="postgres")
    with psycopg.connect(make_conninfo(**admin), autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname='pagw_test'").fetchone():
            c.execute("CREATE DATABASE pagw_test")
    result = base.model_copy(
        update={
            "database_url": base.database_url.__class__(dsn),
            "retry_base_seconds": 0.01,
            "poll_seconds": 0.01,
        }
    )
    Database(result).migrate()
    return result


@pytest.fixture
def store(settings):
    db = Database(settings)
    with db.transaction() as c:
        c.execute(
            "TRUNCATE requests,idempotency,history,subscriptions,deliveries,operations,operation_events,payer_observations,outbox,local_messages,quarantine,inbox,audit,assertion_jtis,mock_authorizations RESTART IDENTITY CASCADE"
        )
    return Store(db, settings)


@pytest.fixture
def broker(store):
    return PostgresBroker(store.db)


@pytest.fixture
def client(store, settings):
    app = create_app(settings)
    app.state.verifier.keys = {
        "demo-idp": PyJWK.from_dict(public_jwk(settings.idp_key_path, "demo-idp")).key
    }
    app.state.verifier.refreshed = time.monotonic()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def token(settings):
    def generate(subject="operator", audience=None, issuer=None, scopes=None):
        return mint(
            settings,
            subject,
            issuer or settings.issuer,
            audience or settings.audience,
            scopes or {"submit", "read", "webhooks", "ops"},
        )["access_token"]

    return generate
