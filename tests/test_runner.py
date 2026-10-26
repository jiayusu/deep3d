import json
from dataclasses import replace

import pytest
from deep3d_cad.cli import main
from deep3d_cad.contracts import CadError, FlangeSpec
from deep3d_cad.jsonio import read_json
from deep3d_cad.runner import file_digest, run_flange, run_worker


def test_reloaded_brep_and_step_have_independent_processes_and_content_hashes(tmp_path):
    out = tmp_path / "nominal"
    result = run_flange(FlangeSpec(), out)
    assert result["status"] == "pass", result
    native = result["stages"]["verify-brep"]
    step = result["stages"]["verify-step"]
    assert native["worker_pid"] != step["worker_pid"]
    assert native["input_artifact_sha256"] == file_digest(out / "flange.brep")
    assert step["input_artifact_sha256"] == file_digest(out / "flange.step")
    assert native["validator_source_sha256"] == step["validator_source_sha256"]
    for artifact in result["artifacts"]:
        assert file_digest(out / artifact["path"]) == artifact["sha256"]
    assert (out / "flange.brep").stat().st_size > 1000
    assert (out / "flange.step").stat().st_size > 1000


def test_expected_goal_is_independent_from_candidate(tmp_path):
    result = run_flange(
        replace(FlangeSpec(), bolt_circle_diameter_mm=80),
        tmp_path / "wrong_hole_positions",
        goal=FlangeSpec(),
    )
    assert result["status"] == "fail", result
    assert result["stages"]["build"]["status"] == "pass"
    assert result["stages"]["verify-brep"]["status"] == "fail"
    assert result["stages"]["verify-step"]["status"] == "fail"


def test_existing_output_is_preserved(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    with pytest.raises(CadError, match="new output directory") as error:
        run_flange(FlangeSpec(), target)
    assert error.value.code == "OUTPUT_EXISTS"
    assert sentinel.read_text() == "original"
    assert list(target.iterdir()) == [sentinel]


def test_timeout_cannot_be_reported_as_success(tmp_path):
    # A real subprocess is started and terminated; no mock kernel result is accepted.
    with pytest.raises(CadError) as error:
        run_worker("build", tmp_path, timeout=0.000001)
    assert error.value.code == "WORKER_TIMEOUT"
    assert not (tmp_path / "build.json").exists()


@pytest.mark.parametrize(
    "text",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e400}',
        '{"x":"\\ud800"}',
        "[]",
        "{broken",
    ],
)
def test_strict_json_rejects_ambiguous_or_invalid_input(tmp_path, text):
    path = tmp_path / "input.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CadError):
        read_json(path)


def test_json_read_has_size_limit(tmp_path):
    path = tmp_path / "huge.json"
    path.write_bytes(b" " * (1_048_576 + 1))
    with pytest.raises(CadError) as error:
        read_json(path)
    assert error.value.code == "INPUT_TOO_LARGE"


@pytest.mark.parametrize("depth", [40, 20_000])
def test_json_depth_rejection_is_structured(tmp_path, depth):
    path = tmp_path / "deep.json"
    path.write_text('{"x":' + "[" * depth + "0" + "]" * depth + "}", encoding="utf-8")
    with pytest.raises(CadError) as error:
        read_json(path)
    assert error.value.code == "SCHEMA_INVALID"


def test_maximum_hole_count_supports_large_topology_reports(tmp_path):
    result = run_flange(FlangeSpec(hole_count=32, bolt_hole_diameter_mm=2), tmp_path / "32holes")
    assert result["status"] == "pass", result


def test_cli_invalid_input_has_nonzero_exit_and_structured_error(tmp_path, capsys):
    path = tmp_path / "goal.json"
    path.write_text('{"units":"m"}', encoding="utf-8")
    assert main(["build", "--spec", str(path), "--out", str(tmp_path / "result")]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "error"
    assert not (tmp_path / "result").exists()
