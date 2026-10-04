import server
from server import ingestion


def test_server_package_imports() -> None:
    assert server.__version__ == "0.0.0"
    assert ingestion is not None
