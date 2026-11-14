"""Entry point: ``python -m codriver_studio [project folder] [--welcome]``."""
import sys


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        from codriver_studio.selftest import run as selftest
        return selftest(sys.argv[2])
    from codriver_studio.ui.app import run
    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
