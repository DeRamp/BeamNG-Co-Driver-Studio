"""Private fakes of the non-UI modules, used only by tests/test_ui_smoke.py while the
real modules are being written. Injected into sys.modules as codriver_studio.<name>."""
NAMES = ["catalog", "dsp", "audio_io", "project", "exporter", "sample_calls"]


def install(names=None):
    import importlib
    import sys
    import codriver_studio
    for name in names or NAMES:
        mod = importlib.import_module(f"ui_fakes.{name}")
        sys.modules[f"codriver_studio.{name}"] = mod
        setattr(codriver_studio, name, mod)
