"""PyInstaller entry point: keep package imports absolute."""
from server.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
