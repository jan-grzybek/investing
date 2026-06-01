"""``python -m investing`` runs the leak-safe production entrypoint."""

from investing.safe_run import _run_main_safely

if __name__ == "__main__":
    _run_main_safely()
