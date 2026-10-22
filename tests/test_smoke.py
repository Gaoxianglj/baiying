import pytest

import companion
from companion.__main__ import main


def test_version_is_set():
    assert companion.__version__ == "0.1.0"


def test_cli_version_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_cli_without_args_prints_help(capsys):
    assert main([]) == 0
    assert "companion" in capsys.readouterr().out
