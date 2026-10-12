"""Fixtures for the plan tests: an in-memory ledger, never the real data directory."""

from collections.abc import Iterator

import pytest
from plan_support import PlanLedger
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from omsync.core.db import Base, make_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def ledger(session: Session) -> PlanLedger:
    return PlanLedger(session)
