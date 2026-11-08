from typer.testing import CliRunner

from qbx.cli import app
from tests.test_pairing import _qp


def test_calibrate_pairs_by_content_and_says_when_nothing_pairs(tmp_path):
    # named nothing like _qp_/_ms_, and with no mark scheme alongside: calibrate pairs by
    # what files are (scan.plan_scan), so it finds no pair here and says so
    _qp(tmp_path / "Download (7).pdf")

    result = CliRunner().invoke(app, ["calibrate", str(tmp_path), "--out", str(tmp_path / "out")])

    assert result.exit_code == 1
    assert "no QP/MS pairs found" in result.output
