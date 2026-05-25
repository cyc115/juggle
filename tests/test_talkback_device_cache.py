"""TDD tests for talkback event-driven device caching.

Covers:
  1. Cached chain reused (no reinit / no sd.query_devices) when _devices_dirty=False.
  2. Dirty flag triggers sd._terminate + sd._initialize + repick + cache + clear.
  3. First call (cache=None, not dirty) builds chain without PortAudio reinit.
  4. _register_device_listener is callable without error when CoreAudio unavailable.

No real CoreAudio / PyObjC required — the dirty flag is injected directly.
Audio arrays are MagicMocks (since _try_play is always mocked here, the type
doesn't matter — numpy is unavailable in the base test runner).
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TALKBACK_PATH = Path(__file__).parent.parent / "scripts" / "talkback"
_SENTINEL = object()


def _load_talkback():
    """Load a fresh talkback module with sounddevice/numpy mocked out.

    Each call returns an independent module object so tests don't share state.
    sys.modules is restored after loading so the MagicMock doesn't pollute
    other tests (same technique as test_talkback.py).
    """
    _saved: dict = {}
    for key in ("sounddevice", "numpy"):
        _saved[key] = sys.modules.get(key, _SENTINEL)
        sys.modules[key] = MagicMock()
    try:
        loader = importlib.machinery.SourceFileLoader("talkback_dc", str(TALKBACK_PATH))
        spec = importlib.util.spec_from_loader("talkback_dc", loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        for key, orig in _saved.items():
            if orig is _SENTINEL:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = orig


@pytest.fixture
def tb():
    """Fresh talkback module per test with sd mock call-counts reset."""
    mod = _load_talkback()
    mod.sd.reset_mock()
    return mod


# ---------------------------------------------------------------------------
# Device list fixtures
# ---------------------------------------------------------------------------

_DEVICES_WITH_AIRPODS = [
    {"name": "AirPods Pro", "max_output_channels": 2},
    {"name": "MacBook Pro Speakers", "max_output_channels": 2},
]

_DEVICES_SPEAKERS_ONLY = [
    {"name": "MacBook Pro Speakers", "max_output_channels": 2},
]


def _configure_devices(mod, devices):
    """Wire sd.query_devices to return a fixed device list."""
    mod.sd.query_devices.return_value = devices
    mod.sd.query_devices.side_effect = None


_FAKE_AUDIO = MagicMock()  # placeholder — _try_play is always mocked


# ---------------------------------------------------------------------------
# 1. Cached chain reused when clean
# ---------------------------------------------------------------------------


def test_cached_chain_reused_when_not_dirty(tb, monkeypatch):
    """When _devices_dirty is False and a cache exists, play on cached device
    without calling sd.query_devices, sd._terminate, or sd._initialize."""
    monkeypatch.setattr(tb, "_devices_dirty", False, raising=False)
    monkeypatch.setattr(tb, "_cached_chain", [2], raising=False)

    played_on = []
    monkeypatch.setattr(tb, "_try_play", lambda _a, _sr, dev: played_on.append(dev))

    tb._play_audio(_FAKE_AUDIO)

    assert played_on == [2], "must reuse cached device without scanning"
    tb.sd._terminate.assert_not_called()
    tb.sd._initialize.assert_not_called()
    tb.sd.query_devices.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Dirty flag triggers reinit + repick + cache + clear
# ---------------------------------------------------------------------------


def test_dirty_flag_triggers_reinit_repick_caches_and_clears(tb, monkeypatch):
    """When _devices_dirty is True: reinit PortAudio, repick, cache chain, clear flag."""
    monkeypatch.setattr(tb, "_devices_dirty", True, raising=False)
    monkeypatch.setattr(tb, "_cached_chain", None, raising=False)
    monkeypatch.setattr(tb, "_cached_device_name", None, raising=False)
    monkeypatch.setattr(tb, "_current_event", None)
    _configure_devices(tb, _DEVICES_WITH_AIRPODS)

    played_on = []
    monkeypatch.setattr(tb, "_try_play", lambda _a, _sr, dev: played_on.append(dev))

    tb._play_audio(_FAKE_AUDIO)

    # PortAudio reinit must have happened
    tb.sd._terminate.assert_called_once()
    tb.sd._initialize.assert_called_once()

    # Flag cleared after reinit
    assert tb._devices_dirty is False

    # Chain built and cached with AirPods (idx 0) at front
    assert tb._cached_chain is not None
    assert tb._cached_chain[0] == 0

    # Actually played on AirPods
    assert played_on == [0]


# ---------------------------------------------------------------------------
# 3. First call builds cache without reinit
# ---------------------------------------------------------------------------


def test_first_call_builds_cache_without_reinit(tb, monkeypatch):
    """First call (cache=None, not dirty) picks devices and caches — no reinit."""
    monkeypatch.setattr(tb, "_devices_dirty", False, raising=False)
    monkeypatch.setattr(tb, "_cached_chain", None, raising=False)
    monkeypatch.setattr(tb, "_cached_device_name", None, raising=False)
    _configure_devices(tb, _DEVICES_SPEAKERS_ONLY)

    played_on = []
    monkeypatch.setattr(tb, "_try_play", lambda _a, _sr, dev: played_on.append(dev))

    tb._play_audio(_FAKE_AUDIO)

    # No PortAudio reinit on cold start
    tb.sd._terminate.assert_not_called()
    tb.sd._initialize.assert_not_called()

    # Chain was built and cached
    assert tb._cached_chain is not None
    assert len(tb._cached_chain) >= 1

    # MacBook Pro Speakers (idx 0) must be in chain
    assert 0 in tb._cached_chain


# ---------------------------------------------------------------------------
# 4. _register_device_listener does not raise when CoreAudio is unavailable
# ---------------------------------------------------------------------------


def test_register_device_listener_no_error_when_unavailable(tb):
    """_register_device_listener silently falls back when CoreAudio cannot be imported."""
    old = sys.modules.get("CoreAudio", _SENTINEL)
    sys.modules["CoreAudio"] = None  # type: ignore[assignment]  # causes ImportError
    try:
        tb._register_device_listener()  # must not raise
    except Exception as exc:
        pytest.fail(f"_register_device_listener raised unexpectedly: {exc!r}")
    finally:
        if old is _SENTINEL:
            sys.modules.pop("CoreAudio", None)
        else:
            sys.modules["CoreAudio"] = old

    # No spurious dirty-set from a failed listener
    assert tb._devices_dirty is False
