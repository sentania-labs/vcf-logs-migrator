import json
from pathlib import Path

import pytest

from logs_migrate.adapters.simulated import SimulatedClock, SimulatedEnvironment
from logs_migrate.engine import Engine
from logs_migrate.journal import Journal
from logs_migrate.models import ACTIVE_ACK, Policy, Window, parse_time

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    return json.loads((FIXTURES / "sealed-populated.fixture").read_text())


@pytest.fixture
def rig(fixture, tmp_path):
    clock = SimulatedClock()
    env = SimulatedEnvironment(fixture)
    journal = Journal(tmp_path / "journal.sqlite", clock)
    engine = Engine(env, journal, clock)
    yield engine, env, journal
    journal.close()


def planned(engine):
    return engine.plan(
        Window(parse_time("2026-03-01T00:00:00Z"), parse_time("2026-04-01T00:00:00Z")),
        Policy(poll_seconds=0),
    )


def execute(engine, run):
    return engine.run(run["id"], backup_ack=True, active_ack=ACTIVE_ACK)


def verified(engine):
    run = planned(engine)
    execute(engine, run)
    return engine.verify(run["id"])
