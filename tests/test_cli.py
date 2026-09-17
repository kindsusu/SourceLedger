import json
from pathlib import Path

from su_crawler.cli import main


def test_research_cli_workflow(tmp_path, capsys):
    path = tmp_path / "research.json"
    workspace = ["--workspace", str(path)]
    assert main(["init", *workspace, "--industry", "Components", "--product", "Test part", "--market", "Korea"]) == 0
    assert json.loads(capsys.readouterr().out)["locale"] == "en"
    assert main(["source-add", *workspace, "--url", "https://example.com/item#price"]) == 0
    capsys.readouterr()
    assert main(["product-set", *workspace, "--identifier", "model=TEST-A", "--spec", "grade=A"]) == 0
    capsys.readouterr()
    draft = tmp_path / "draft.json"
    assert main(["draft", *workspace, "--output", str(draft)]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is False
    config = json.loads(draft.read_text(encoding="utf-8"))
    assert config["products"][0]["identifiers"] == {"model": "TEST-A"}
    assert config["products"][0]["required_specs"] == {"grade": "A"}
    assert config["sources"][0]["location"] == "https://example.com/item"
    assert main(["research-status", *workspace]) == 0
    assert "search_provider_unconfigured" in capsys.readouterr().out


def test_first_run_korean_prompts_and_no_analysis_purpose(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    entries = iter(["부품", "나사", "한국"])
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return next(entries)

    monkeypatch.setattr("builtins.input", respond)
    assert main(["init", "--workspace", str(tmp_path / "research.json"), "--lang", "ko"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["locale"] == "ko"
    assert prompts == ["산업군: ", "상품 또는 상품군: ", "대상 시장 또는 지역: "]
    assert "analysis_purpose" not in payload


def test_invalid_noninteractive_setup_and_duplicate_identifiers(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    path = tmp_path / "research.json"
    assert main(["init", "--workspace", str(path)]) == 2
    assert "--industry" in capsys.readouterr().err
    assert not path.exists()
    assert main(["product-set", "--workspace", str(path), "--identifier", "model=A", "--identifier", "model=B"]) == 2
    assert "KEY=VALUE" in capsys.readouterr().err


def test_cli_verify_activate_round_trip(tmp_path, capsys):
    root = Path(__file__).resolve().parents[1]
    raw = json.loads((root / "examples/verification.json").read_text(encoding="utf-8"))
    raw["output_dir"] = str(tmp_path / "outputs")
    for source in raw["sources"]:
        source["location"] = str(root / "examples" / source["location"])
        source["file_root"] = str(root / "examples")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    assert main(["verify", "--config", str(path), "--receipt", str(receipt), "--samples", str(root / "examples/verification.samples.json")]) == 0
    assert json.loads(capsys.readouterr().out)["eligible"] is True
    active = tmp_path / "active.json"
    assert main(["activate", "--config", str(path), "--receipt", str(receipt), "--output", str(active)]) == 0
    capsys.readouterr()
    assert main(["run", "--config", str(active)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert Path(result["report_path"]).is_file()


def test_cli_failed_verification_returns_nonzero(tmp_path, capsys):
    root = Path(__file__).resolve().parents[1]
    raw = json.loads((root / "examples/verification.json").read_text(encoding="utf-8"))
    raw["products"][0]["identifiers"] = {"model": "UNKNOWN"}
    raw["output_dir"] = str(tmp_path / "outputs")
    for source in raw["sources"]:
        source["location"] = str(root / "examples" / source["location"])
        source["file_root"] = str(root / "examples")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert main(["verify", "--config", str(path), "--receipt", str(tmp_path / "receipt.json")]) == 1
    assert json.loads(capsys.readouterr().out)["eligible"] is False
