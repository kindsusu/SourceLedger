import json
import os
import sys
import time

import pytest

from su_crawler import ai_providers as ai


REQUEST = {"query": "industrial pumps", "kind": "keyword", "topic": {
    "industry": "manufacturing", "market": "Korea", "product": {"id": "product", "name": "pump"}}}
RESULT = {"candidates": [{"name": "Example", "url": "https://example.com/",
                          "reason": "Product page", "evidence_url": "https://example.com/pumps"}], "note": ""}
PLAN = {"request_text": "Find Acme pumps in Korea.\nExclude used goods.",
        "topic": {"industry": "manufacturing", "product": "pump", "market": "Korea"},
        "summary": "User-edited Acme pump scope", "categories": ["Acme pumps"],
        "include_terms": ["Acme"], "exclude_terms": ["used"],
        "candidates": [{"name": "Manual Acme site", "url": "https://manual.example.com/pumps",
                        "evidence_url": "https://manual.example.com/pumps", "reason": "User added",
                        "kind": "site", "origin": "user", "selected": False}],
        "excluded_urls": ["https://excluded.example.com/catalog"]}
PREVIEW = {"summary": "Acme pump sources", "topic": PLAN["topic"], "categories": ["Acme catalog"],
           "include_terms": ["Acme"], "exclude_terms": ["used"],
           "candidates": [{"name": "Acme catalog", "url": "https://example.com/pumps",
                           "evidence_url": "https://example.com/search", "reason": "Public catalog",
                           "kind": "site"}], "note": "Review before collection"}


@pytest.mark.skipif(os.name != "nt", reason="Windows npm prefix resolution")
def test_codex_custom_npm_prefix_uses_node_without_shell_shim(tmp_path, monkeypatch):
    prefix = tmp_path / "custom prefix"
    entry = prefix / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// fixture", encoding="utf-8")
    (prefix / "node.exe").write_bytes(b"fixture")
    shim = prefix / "codex.cmd"
    shim.write_text("exit 99", encoding="utf-8")
    monkeypatch.setattr(ai.shutil, "which", lambda value: str(shim) if value == "codex" else None)
    assert ai._codex_command() == [str(prefix / "node.exe"), str(entry)]


def test_settings_defaults_validation_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "_codex_command", lambda: None)
    monkeypatch.setattr(ai, "_claude_command", lambda: None)
    assert ai.get_ai_configuration(tmp_path)["settings"] == {"provider": "codex", "model": "", "timeout_seconds": 180}
    saved = ai.save_ai_settings(tmp_path, {"provider": "claude", "model": "sonnet", "timeout_seconds": 210})
    assert saved["settings"] == ai.get_ai_configuration(tmp_path)["settings"]
    assert json.loads((tmp_path / "ai-settings.json").read_text(encoding="utf-8")) == saved["settings"]
    for bad in ({"provider": "other"}, {"model": "-unsafe"}, {"model": "bad name"},
                {"model": "a" * 201}, {"timeout_seconds": True}, {"timeout_seconds": 29},
                {"timeout_seconds": 601}, {"command": "echo"}):
        with pytest.raises(ValueError):
            ai.normalize_ai_settings(bad)


def test_settings_symlink_and_oversize_rejected(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path = tmp_path / "ai-settings.json"
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="unsafe"):
        ai.save_ai_settings(tmp_path, {})
    assert outside.read_text(encoding="utf-8") == "{}"
    path.unlink()
    path.write_text(" " * (ai.MAX_SETTINGS_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        ai.get_ai_configuration(tmp_path)


def test_local_models_cache_is_suggestion_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "local-model", "display_name": "Local model"},
        {"slug": "-bad", "display_name": "Bad"},
        {"slug": "has space", "display_name": "Bad"}]}), encoding="utf-8")
    assert ai._codex_models() == [{"id": "local-model", "label": "Local model"}]


def test_codex_command_and_result_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "_codex_command", lambda: [sys.executable, "codex.js"])
    monkeypatch.setattr(ai, "_check_help", lambda command, provider: None)
    captured = {}

    def fake_run(command, prompt, *, cwd, timeout):
        captured.update(command=command, prompt=prompt, timeout=timeout)
        (cwd / "last-message.json").write_text(json.dumps(RESULT), encoding="utf-8")
        return 0, b"provider events omitted"

    monkeypatch.setattr(ai, "_run_cli", fake_run)
    result = ai.generate_recommendations(REQUEST, {"provider": "codex", "model": "sample-model"})
    assert result["provider"] == "codex" and result["requested_model"] == "sample-model"
    assert result["actual_model"] is None and result["candidates"][0]["url"] == "https://example.com/"
    cmd = captured["command"]
    assert cmd[-3:] == ["-m", "sample-model", "-"]
    assert "--ignore-user-config" in cmd and "--sandbox" in cmd and "read-only" in cmd
    disabled = {cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "--disable"}
    assert set(ai.CODEX_FEATURES) <= disabled
    assert not {"code_mode", "code_mode_host"} & disabled
    assert "web_search='live'" in cmd
    assert "industrial pumps" in captured["prompt"] and "sourceledger-ai-" not in captured["prompt"]


def test_claude_command_and_structured_output(monkeypatch):
    monkeypatch.setattr(ai, "_claude_command", lambda: [sys.executable, "claude.py"])
    monkeypatch.setattr(ai, "_check_help", lambda command, provider: None)
    captured = {}

    def fake_run(command, prompt, *, cwd, timeout):
        captured["command"] = command
        return 0, json.dumps({"is_error": False, "structured_output": RESULT,
                              "modelUsage": {"claude-model-xyz": {"inputTokens": 1}}}).encode()

    monkeypatch.setattr(ai, "_run_cli", fake_run)
    result = ai.generate_recommendations(REQUEST, {"provider": "claude", "model": "opus"})
    assert result["provider"] == "claude" and len(result["candidates"]) == 1
    assert result["actual_model"] == "claude-model-xyz"
    cmd = captured["command"]
    assert cmd[-2:] == ["--model", "opus"]
    for flag in ("--safe-mode", "--no-session-persistence", "--tools", "--allowedTools", "--disallowedTools"):
        assert flag in cmd


@pytest.mark.parametrize("event, expected_code", [
    ({"type": "item.completed", "item": {"type": "error", "message":
        "Code Mode is unavailable because code-mode host is disabled. PRIVATE DIAGNOSTIC"}}, "cli_tools_unavailable"),
    ({"type": "turn.failed", "error": {"message": "PRIVATE DIAGNOSTIC"}}, "cli_failed"),
])
def test_codex_runtime_error_is_not_accepted_as_a_successful_preview(event, expected_code, monkeypatch):
    monkeypatch.setattr(ai, "_codex_command", lambda: ["codex"])
    monkeypatch.setattr(ai, "_check_help", lambda *args: None)

    def fake_run(command, prompt, *, cwd, timeout):
        (cwd / "last-message.json").write_text(json.dumps(PREVIEW), encoding="utf-8")
        return 0, json.dumps(event).encode()

    monkeypatch.setattr(ai, "_run_cli", fake_run)
    with pytest.raises(ai.AIGenerationError) as failure:
        ai.generate_plan_preview(PLAN, {"provider": "codex"})
    assert failure.value.code == expected_code
    assert "PRIVATE DIAGNOSTIC" not in failure.value.safe_message


def test_codex_model_text_is_not_a_runtime_error():
    ai._check_codex_events(b'not an event\n' + json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "Code Mode is unavailable"}}).encode())


def test_invalid_output_is_safe_and_rejects_unverified_links(monkeypatch):
    monkeypatch.setattr(ai, "_claude_command", lambda: [sys.executable])
    monkeypatch.setattr(ai, "_check_help", lambda command, provider: None)
    bad = {"candidates": [{**RESULT["candidates"][0], "url": "http://localhost/private"}], "note": ""}
    monkeypatch.setattr(ai, "_run_cli", lambda *args, **kwargs: (0, json.dumps({"structured_output": bad}).encode()))
    with pytest.raises(ai.AIGenerationError) as raised:
        ai.generate_recommendations(REQUEST, {"provider": "claude"})
    assert raised.value.code == "invalid_response" and "localhost" not in raised.value.safe_message
    monkeypatch.setattr(ai, "_run_cli", lambda *args, **kwargs: (1, b"SECRET FROM STDERR"))
    with pytest.raises(ai.AIGenerationError) as raised:
        ai.generate_recommendations(REQUEST, {"provider": "claude"})
    assert "SECRET" not in str(raised.value)


def test_real_subprocess_output_limit_and_timeout(tmp_path):
    huge = [sys.executable, "-c", "import sys; sys.stdin.read(); sys.stdout.write('x' * 1200000)"]
    with pytest.raises(ai.AIGenerationError) as large:
        ai._run_cli(huge, "small", cwd=tmp_path, timeout=3)
    assert large.value.code == "output_limit"
    slow = [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(5)"]
    with pytest.raises(ai.AIGenerationError) as timed:
        ai._run_cli(slow, "small", cwd=tmp_path, timeout=1)
    assert timed.value.code == "timeout"


def test_real_subprocess_blocked_stdin_is_timed(tmp_path):
    blocked = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.monotonic()
    with pytest.raises(ai.AIGenerationError) as timed:
        ai._run_cli(blocked, "x" * (2 * 1024 * 1024), cwd=tmp_path, timeout=1)
    assert timed.value.code == "timeout"
    assert time.monotonic() - started < 8


def test_real_subprocess_exited_parent_descendant_cleanup(tmp_path):
    child = "import time; time.sleep(30)"
    parent = ("import subprocess,sys; "
              "subprocess.Popen([sys.executable,'-c'," + repr(child) + "], "
              "stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=subprocess.DEVNULL); "
              "sys.stdout.write('parent done\\n')")
    started = time.monotonic()
    code, output = ai._run_cli([sys.executable, "-c", parent], "", cwd=tmp_path, timeout=3)
    assert code == 0 and b"parent done" in output
    assert time.monotonic() - started < 8


def test_claude_hidden_max_turns_preflight(monkeypatch):
    help_text = "--json-schema --no-session-persistence --safe-mode --tools " \
                "--allowedTools --disallowedTools --permission-mode --output-format"
    calls = []

    def fake_run(command, prompt, *, cwd, timeout):
        calls.append(command[-1])
        return (0, help_text.encode()) if command[-1] == "--help" else (0, b"2.1.284 (Claude Code)")

    monkeypatch.setattr(ai, "_run_cli", fake_run)
    ai._check_help(["claude"], "claude")
    assert calls == ["--help", "--version"]

    monkeypatch.setattr(ai, "_run_cli", lambda command, prompt, *, cwd, timeout:
                        (0, help_text.encode()) if command[-1] == "--help" else (0, b"1.0.0"))
    with pytest.raises(ai.AIGenerationError) as old:
        ai._check_help(["claude"], "claude")
    assert old.value.code == "cli_unsupported"


def test_claude_missing_visible_restriction_preflight(monkeypatch):
    monkeypatch.setattr(ai, "_run_cli", lambda *args, **kwargs: (0, b"--json-schema --safe-mode"))
    with pytest.raises(ai.AIGenerationError) as unsupported:
        ai._check_help(["claude"], "claude")
    assert unsupported.value.code == "cli_unsupported"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_plan_preview_uses_restricted_structured_cli(provider, monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_codex_command" if provider == "codex" else "_claude_command",
                        lambda: [sys.executable, "fake-cli"])
    monkeypatch.setattr(ai, "_check_help", lambda command, selected: None)
    captured = {}

    def fake_run(command, prompt, *, cwd, timeout):
        captured.update(command=command, prompt=prompt, timeout=timeout)
        if provider == "codex":
            (cwd / "last-message.json").write_text(json.dumps(PREVIEW), encoding="utf-8")
            return 0, b""
        return 0, json.dumps({"structured_output": PREVIEW,
                              "modelUsage": {"claude-model-xyz": {"inputTokens": 1}}}).encode()

    monkeypatch.setattr(ai, "_run_cli", fake_run)
    result = ai.generate_plan_preview(PLAN, {"provider": provider, "model": "sample-model"})
    assert result["summary"] == PREVIEW["summary"]
    assert result["candidates"] == PREVIEW["candidates"]
    assert result["actual_model"] == ("claude-model-xyz" if provider == "claude" else None)
    prompt_data = json.loads(captured["prompt"].split("User request and current edits JSON follows:\n", 1)[1])
    assert prompt_data == PLAN
    assert "Do not turn natural-language conditions into literal terms" in captured["prompt"]
    assert "no exclude term may match" in captured["prompt"]
    assert "Do not collect prices" in captured["prompt"]
    from su_crawler.research_intent import INTENT_GUIDANCE
    assert INTENT_GUIDANCE in captured["prompt"]
    assert "--sandbox" in captured["command"] if provider == "codex" else "--safe-mode" in captured["command"]
    from su_crawler.research_plans import create_plan, submit_plan_preview

    draft = create_plan(tmp_path, PLAN["request_text"], PLAN["include_terms"], PLAN["exclude_terms"])["plan"]
    preview = {key: value for key, value in result.items()
               if key not in {"provider", "requested_model", "actual_model"}}
    saved = submit_plan_preview(tmp_path, draft["id"], draft["revision"], preview)["plan"]
    assert saved["state"] == "preview" and saved["candidates"][0]["origin"] == "ai"


@pytest.mark.parametrize("bad", [
    {**PREVIEW, "price": "100"},
    {**PREVIEW, "candidates": [{**PREVIEW["candidates"][0], "currency": "KRW"}]},
    {**PREVIEW, "candidates": [{**PREVIEW["candidates"][0], "evidence_url": "http://localhost/"}]},
    {**PREVIEW, "topic": {"industry": "manufacturing"}},
])
def test_plan_preview_rejects_malformed_or_price_fields(bad, monkeypatch):
    monkeypatch.setattr(ai, "_claude_command", lambda: [sys.executable])
    monkeypatch.setattr(ai, "_check_help", lambda command, selected: None)
    monkeypatch.setattr(ai, "_run_cli", lambda *args, **kwargs:
                        (0, json.dumps({"structured_output": bad}).encode()))
    with pytest.raises(ai.AIGenerationError) as error:
        ai.generate_plan_preview(PLAN, {"provider": "claude"})
    assert error.value.code == "invalid_response"
    assert "localhost" not in str(error.value)
