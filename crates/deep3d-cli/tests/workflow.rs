//! Real fresh-process save/reopen/edit/OCCT checks, not in-memory serialization tests.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use uuid::Uuid;

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_deep3d")
}

fn python() -> PathBuf {
    if let Some(path) = std::env::var_os("DEEP3D_TEST_PYTHON") {
        return PathBuf::from(path);
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let candidate = root.join(if cfg!(windows) {
        ".venv/Scripts/python.exe"
    } else {
        ".venv/bin/python"
    });
    assert!(
        candidate.is_file(),
        "Run uv sync --frozen or set DEEP3D_TEST_PYTHON for real OCCT integration tests"
    );
    std::path::absolute(candidate).unwrap()
}

fn process(args: &[&str]) -> Output {
    Command::new(binary()).args(args).output().unwrap()
}

fn success(args: &[&str]) -> Value {
    let output = process(args);
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["status"], "pass");
    value["result"].clone()
}

fn failure(args: &[&str]) -> Value {
    let output = process(args);
    assert_eq!(output.status.code(), Some(2));
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["status"], "error");
    value["error"].clone()
}

fn thickness(snapshot: &Value) -> f64 {
    snapshot["project"]["parameters"]
        .as_object()
        .unwrap()
        .values()
        .find(|p| p["name"] == "thickness_mm")
        .unwrap()["value"]["value"]
        .as_f64()
        .unwrap()
}

#[test]
fn native_project_reopens_edits_recomputes_and_exports_both_versions() {
    let directory = tempfile::tempdir().unwrap();
    let project_path = directory.path().join("flange.deep3d");
    let path = project_path.to_str().unwrap();
    let python = python();
    let interpreter = python.to_str().unwrap();
    let initial = success(&["create", "--project", path, "--name", "Flange"]);
    let revision = initial["revision"]["id"].as_str().unwrap();
    let original_ids = initial["project"]["features"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    let out0 = directory.path().join("v0");
    let evaluated0 = success(&[
        "evaluate",
        "--project",
        path,
        "--python",
        interpreter,
        "--out",
        out0.to_str().unwrap(),
    ]);
    assert_eq!(evaluated0["revision_id"], revision);
    assert_eq!(evaluated0["head"]["generation"], "0");

    let edited = success(&[
        "edit",
        "--project",
        path,
        "--set",
        "thickness_mm=16",
        "--set",
        "bolt_circle_diameter_mm=80",
    ]);
    assert_eq!(thickness(&edited), 16.0);
    assert_eq!(edited["head"]["generation"], "1");
    assert_eq!(edited["revision"]["parent"], revision);
    assert_eq!(
        edited["project"]["features"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<Vec<_>>(),
        original_ids
    );
    let out1 = directory.path().join("v1");
    let evaluated1 = success(&[
        "evaluate",
        "--project",
        path,
        "--python",
        interpreter,
        "--out",
        out1.to_str().unwrap(),
    ]);
    assert_ne!(evaluated0["materialization"], evaluated1["materialization"]);
    let old = success(&["show", "--project", path, "--revision", revision]);
    assert_eq!(thickness(&old["snapshot"]), 12.0);
    assert_eq!(old["materializations"].as_array().unwrap().len(), 1);
    assert_eq!(
        success(&["history", "--project", path])
            .as_array()
            .unwrap()
            .len(),
        2
    );
    let measured: Value =
        serde_json::from_slice(&fs::read(out1.join("verify-step.json")).unwrap()).unwrap();
    let z_max = measured["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == "outer.z_max")
        .unwrap();
    assert_eq!(z_max["observed"], 16.0);

    // Move only the native package to a new location. Export reads objects
    // inside that package; neither Python nor evaluation directories are used.
    let portable = tempfile::tempdir().unwrap();
    let copied = portable.path().join("portable.deep3d");
    fs::copy(&project_path, &copied).unwrap();
    for (index, evaluation) in [evaluated0, evaluated1].iter().enumerate() {
        let exported = portable.path().join(format!("v{index}.step"));
        success(&[
            "export",
            "--project",
            copied.to_str().unwrap(),
            "--materialization",
            evaluation["materialization"].as_str().unwrap(),
            "--out",
            exported.to_str().unwrap(),
        ]);
        assert_eq!(
            fs::read(exported).unwrap(),
            fs::read(directory.path().join(format!("v{index}/flange.step"))).unwrap()
        );
    }
    let before = fs::read(&project_path).unwrap();
    let rejected = failure(&["edit", "--project", path, "--set", "thickness_mm=-1"]);
    assert_eq!(rejected["code"], "OUTSIDE_QUALIFIED_ENVELOPE");
    assert_eq!(fs::read(&project_path).unwrap(), before);

    let current = success(&["show", "--project", path]);
    let mut feature = current["snapshot"]["project"]["features"]
        .as_object()
        .unwrap()
        .values()
        .find(|f| f["name"] == "bolt_hole_0_profile")
        .unwrap()
        .clone();
    feature["operation"]["radius"] =
        json!({"kind":"literal","value":{"kind":"length_mm","value":5.0}});
    let transaction = json!({
        "id":Uuid::new_v4(),"schema_version":"deep3d.transaction/1",
        "base_revision":current["snapshot"]["revision"]["id"],
        "expected_generation":current["snapshot"]["head"]["generation"],
        "message":"Deliberately wrong modeled hole, unchanged design goal",
        "commands":[{"op":"feature.replace","feature":feature}],
    });
    let transaction_file = directory.path().join("wrong.json");
    fs::write(&transaction_file, serde_json::to_vec(&transaction).unwrap()).unwrap();
    success(&[
        "apply",
        "--project",
        path,
        "--transaction",
        transaction_file.to_str().unwrap(),
    ]);
    let before = fs::read(&project_path).unwrap();
    let failed_out = directory.path().join("wrong-geometry");
    assert_eq!(
        failure(&[
            "evaluate",
            "--project",
            path,
            "--python",
            interpreter,
            "--out",
            failed_out.to_str().unwrap()
        ])["code"],
        "EVALUATION_FAILED"
    );
    assert_eq!(fs::read(&project_path).unwrap(), before);
    let current = success(&["show", "--project", path]);
    assert!(current["materializations"].as_array().unwrap().is_empty());
}

#[test]
fn a_slow_old_revision_result_never_overwrites_a_new_edit() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("race.deep3d");
    let path_str = path.to_str().unwrap();
    let original = success(&["create", "--project", path_str]);
    let output = directory.path().join("old-evaluation");
    let python = python();
    let mut child = Command::new(binary())
        .args([
            "evaluate",
            "--project",
            path_str,
            "--python",
            python.to_str().unwrap(),
            "--out",
            output.to_str().unwrap(),
        ])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    while !output.join("plan.json").exists() {
        assert!(
            Instant::now() < deadline,
            "Worker did not materialize its frozen plan"
        );
        assert!(
            child.try_wait().unwrap().is_none(),
            "Evaluation terminated before it started"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    let edited = success(&["edit", "--project", path_str, "--set", "thickness_mm=18"]);
    let result = child.wait_with_output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );
    let evaluation: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(
        evaluation["result"]["revision_id"],
        original["revision"]["id"]
    );
    assert_eq!(
        evaluation["result"]["head"]["revision_id"],
        edited["revision"]["id"]
    );
    let current = success(&["show", "--project", path_str]);
    assert_eq!(thickness(&current["snapshot"]), 18.0);
    assert!(current["materializations"].as_array().unwrap().is_empty());
    let old = success(&[
        "show",
        "--project",
        path_str,
        "--revision",
        original["revision"]["id"].as_str().unwrap(),
    ]);
    assert_eq!(old["materializations"].as_array().unwrap().len(), 1);
}
