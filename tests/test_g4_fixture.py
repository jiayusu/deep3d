import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("g4_benchmark", ROOT / "scripts" / "g4_benchmark.py")
g4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g4)


def test_registered_graph_matches_the_textual_definition():
    edges = g4.registered_edges()
    parents = {}
    for parent, child in edges:
        parents.setdefault(child, set()).add(parent)
    assert not any(child < 100 for child in parents)
    assert parents[100] == {0}
    assert parents[101] == {1, 100}
    assert parents[700] == {0, 699, 599}
    assert len(edges) == len(set(edges))


def test_oracle_closure_and_reasons_on_a_hand_checked_edit():
    result = g4.oracle(g4.registered_edges(), 998)
    assert result["dirty"] == ["f0998", "f0999"]
    assert result["reasons"] == {"f0999": ["f0998"]}
    root = g4.oracle(g4.registered_edges(), 5)
    assert root["dirty"][:2] == ["f0005", "f0105"]
    assert "f0004" not in root["dirty"] and len(root["dirty"]) == 1 + 895


def test_committed_manifest_is_reproduced_by_the_generator(tmp_path):
    committed = json.loads((ROOT / "acceptance" / "v0.1" / "g4-fixture.json").read_text())
    assert g4.generate(tmp_path) == committed
