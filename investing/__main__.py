"""``python -m investing`` runs the leak-safe production entrypoint."""

import sys

from investing.safe_run import _run_main_safely, _run_snapshot_safely

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "snapshot":
        _run_snapshot_safely()
    else:
        _run_main_safely()
