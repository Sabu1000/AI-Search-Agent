import json
from unittest.mock import patch

import pytest

from universal_ai_search.worker.main import main, run


def test_worker_configuration_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--check"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "environment": "local",
        "service": "worker",
        "status": "ok",
        "version": "0.1.0",
    }


def test_worker_consumes_one_job() -> None:
    with patch(
        "universal_ai_search.worker.main.IndexingRuntime.run_once", return_value=False
    ) as run_once:
        assert main(["--once"]) == 0

    run_once.assert_called_once()


def test_worker_rejects_invalid_poll_interval() -> None:
    with pytest.raises(SystemExit, match="greater than zero"):
        main(["--poll-interval", "0"])


def test_worker_console_entrypoint_returns_main_status() -> None:
    with (
        patch("universal_ai_search.worker.main.main", return_value=0),
        pytest.raises(SystemExit) as exit_info,
    ):
        run()

    assert exit_info.value.code == 0
