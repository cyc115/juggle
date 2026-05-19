import pytest
from unittest.mock import Mock, patch


@pytest.fixture
def mock_db():
    db = Mock()
    db.init_db = Mock()
    return db


@pytest.fixture
def mock_get_db(mock_db):
    with patch("juggle_cmd_context.get_db", return_value=mock_db):
        yield mock_db


def test_cmd_get_context_calls_build_context_string(tmp_path):
    from juggle_cmd_context import cmd_get_context

    mock_db_path = tmp_path / "test.db"

    with patch("juggle_cmd_context.DB_PATH", mock_db_path):
        with patch("juggle_cmd_context.SRC_DIR", tmp_path):
            with patch("juggle_context.build_context_string", return_value="context output") as mock_build:
                with patch("builtins.print") as mock_print:
                    cmd_get_context(None)

    mock_build.assert_called_once_with(db_path=str(mock_db_path))
    mock_print.assert_called_once_with("context output")


def test_cmd_get_context_prints_result(tmp_path):
    from juggle_cmd_context import cmd_get_context

    test_output = "test context result"

    with patch("juggle_cmd_context.DB_PATH", tmp_path / "db"):
        with patch("juggle_cmd_context.SRC_DIR", tmp_path):
            with patch("juggle_context.build_context_string", return_value=test_output):
                with patch("builtins.print") as mock_print:
                    cmd_get_context(None)

    mock_print.assert_called_with(test_output)


def test_cmd_init_db_creates_data_dir(mock_get_db, tmp_path):
    from juggle_cmd_context import cmd_init_db

    with patch("juggle_cmd_context._DATA_DIR", tmp_path / "data"):
        with patch("builtins.print") as mock_print:
            cmd_init_db(None)

    assert (tmp_path / "data").exists()
    mock_get_db.init_db.assert_called_once()
    mock_print.assert_called_once_with("DB initialized.")


def test_cmd_init_db_calls_db_init(mock_get_db):
    from juggle_cmd_context import cmd_init_db

    with patch("juggle_cmd_context._DATA_DIR"):
        with patch("builtins.print"):
            cmd_init_db(None)

    mock_get_db.init_db.assert_called_once()


def test_cmd_init_db_prints_confirmation(mock_get_db, tmp_path):
    from juggle_cmd_context import cmd_init_db

    with patch("juggle_cmd_context._DATA_DIR", tmp_path / "data"):
        with patch("builtins.print") as mock_print:
            cmd_init_db(None)

    mock_print.assert_called_with("DB initialized.")
