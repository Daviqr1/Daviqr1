import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))


@pytest.fixture(autouse=True, scope="session")
def _no_cache_under_home(tmp_path_factory):
    # neurokit2 (optional cross-check in test_signal) imports matplotlib, which writes its config and
    # font cache under ~ unless MPLCONFIGDIR says otherwise
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MPLCONFIGDIR", str(tmp_path_factory.mktemp("matplotlib")))
        yield


@pytest.fixture(autouse=True)
def _own_pulso_home(tmp_path, monkeypatch):
    # cli.main and notebook.run create PULSO_HOME (default ~/.pulso): never the real one of whoever runs
    # the tests, nor a database or FIT archive their shell points at
    monkeypatch.setenv("PULSO_HOME", str(tmp_path / "pulso-home"))
    monkeypatch.delenv("PULSO_DB", raising=False)
    monkeypatch.delenv("PULSO_ARCHIVE", raising=False)
