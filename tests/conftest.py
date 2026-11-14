import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_dsp():
    """Use the real codriver_studio.dsp when importable; otherwise (or with CDS_FAKE_DSP=1)
    inject tests/fakes/fake_dsp.py as codriver_studio.dsp. Must run before the project
    modules are imported, hence at conftest import time."""
    import codriver_studio
    if os.environ.get("CDS_FAKE_DSP") != "1":
        try:
            import codriver_studio.dsp  # noqa: F401
            return "real"
        except ImportError:
            pass
    spec = importlib.util.spec_from_file_location("codriver_studio.dsp", Path(__file__).parent / "fakes" / "fake_dsp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["codriver_studio.dsp"] = mod
    spec.loader.exec_module(mod)
    codriver_studio.dsp = mod
    return "fake"


DSP_KIND = _install_dsp()


def pytest_report_header(config):
    return f"codriver_studio.dsp under test: {DSP_KIND}"


@pytest.fixture
def dsp_kind():
    return DSP_KIND
