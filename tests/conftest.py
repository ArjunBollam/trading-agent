from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import Clock, FakeBroker, FakeLLM, RecordingChannel, seed_market  # noqa: E402
from paperbot.app import build_app  # noqa: E402
from paperbot.config import Config  # noqa: E402
from paperbot.llm import AnthropicLLM  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    cfg = Config.load(ROOT / "config.yaml")
    cfg.log_dir = tmp_path / "logs"
    cfg.llm.provider = "anthropic"  # tests that need another provider set it themselves
    return cfg


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def broker(clock):
    b = FakeBroker(clock)
    seed_market(b)
    return b


@pytest.fixture
def channel():
    return RecordingChannel()


@pytest.fixture
def make_app(cfg, broker, channel, clock):
    def make(llm=None, dry_run=False):
        if llm is None or isinstance(llm, FakeLLM):
            llm = AnthropicLLM(llm or FakeLLM(), cfg.llm.anthropic)
        return build_app(cfg, broker, llm, channel, dry_run=dry_run, now=clock)
    return make


@pytest.fixture
def app(make_app):
    return make_app()
