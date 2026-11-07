import server
from server import ingestion


def test_server_package_imports() -> None:
    assert server.__version__ == "0.1.1"
    assert ingestion is not None
