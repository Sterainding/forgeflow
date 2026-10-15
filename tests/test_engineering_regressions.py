"""Behavioral regressions for MCP binding, scoring, transfer and resource cleanup."""
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent import main
from eval import exec_scorer
from sandbox import docker_manager, docker_sandbox, manager


@pytest.mark.asyncio
@pytest.mark.parametrize("rag_enabled", [False, True])
async def test_mcp_tools_reach_agent_with_and_without_rag(monkeypatch, rag_enabled: bool) -> None:
    """Loaded MCP tools must be bound even when RAG is disabled."""
    mcp_tool = object()
    retrieval, experience = object(), object()
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(rag_enabled=rag_enabled))
    monkeypatch.setattr(main.MCPManager, "from_settings", lambda: SimpleNamespace(get_tools=AsyncMock(return_value=[mcp_tool])))
    monkeypatch.setattr(main, "build_model", lambda: object())
    monkeypatch.setattr(main, "build_subagents", lambda path: [])
    for name in ["RequestContextMiddleware", "ToolAuditMiddleware", "CostMeterMiddleware", "LLMConcurrencyMiddleware", "build_fallback_middleware"]:
        monkeypatch.setattr(main, name, lambda **kwargs: object())
    from infra import rag
    monkeypatch.setattr(rag, "collection_for", lambda path: "test-collection")
    monkeypatch.setattr(rag, "build_retrieve_tool", lambda collection: retrieval)
    monkeypatch.setattr(rag, "build_experience_tool", lambda: experience)
    factory = Mock(return_value=object())
    monkeypatch.setattr(main, "create_deep_agent", factory)
    await main.build_agent(object(), "/home/agent", checkpointer=object())
    expected = [mcp_tool, retrieval, experience] if rag_enabled else [mcp_tool]
    assert factory.call_args.kwargs["tools"] == expected


@pytest.mark.asyncio
async def test_cleanup_runs_when_export_fails(monkeypatch) -> None:
    """Export failure is reported without leaking its runtime container."""
    sandbox = docker_sandbox.DockerSandbox("fake-container")
    runtime = main.AgentRuntime(agent=object(), sandbox=sandbox, client=None)
    monkeypatch.setattr(main, "create_agent_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(docker_manager, "export_sandbox_changes", AsyncMock(side_effect=RuntimeError("export failed")))
    cleanup = AsyncMock()
    monkeypatch.setattr(manager, "cleanup_sandbox", cleanup)
    with pytest.raises(RuntimeError, match="export failed"):
        async with main.agent_runtime_session():
            pass
    cleanup.assert_awaited_once_with(sandbox)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_step", [0, 1])
async def test_export_errors_are_not_silently_swallowed(monkeypatch, tmp_path: Path, failed_step: int) -> None:
    """A failed container tar or host extraction must propagate to the caller."""
    outputs = [SimpleNamespace(returncode=0, stdout=b"archive", stderr=b"")]
    failure = SimpleNamespace(returncode=1, stdout=b"", stderr=b"disk error")
    outputs = [failure] if failed_step == 0 else outputs + [failure]
    run = Mock(side_effect=outputs)
    monkeypatch.setattr(docker_manager.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="disk error"):
        await docker_manager.export_sandbox_changes(docker_sandbox.DockerSandbox("fake"), tmp_path)
    assert run.call_count == failed_step + 1


def test_download_preserves_binary_bytes_and_literal_path(monkeypatch) -> None:
    """No shell interpolation or text decoding is used for downloads."""
    payload = b"\x00\xff\n\r\n"
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=payload, stderr=b""))
    monkeypatch.setattr(docker_sandbox.subprocess, "run", run)
    path = "/home/agent/a file;$(echo nope).bin"
    result = docker_sandbox.DockerSandbox("fake").download_files([path])
    assert result[0].content == payload
    assert run.call_args.args[0] == ["docker", "exec", "fake", "cat", "--", path]


def test_download_failure_does_not_contaminate_contents(monkeypatch) -> None:
    """Missing files return a per-file error, never stderr as file contents."""
    monkeypatch.setattr(docker_sandbox.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=b"not found")))
    result = docker_sandbox.DockerSandbox("fake").download_files(["/missing"])
    assert result[0].content is None
    assert result[0].error == "file_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,output,score", [(0, "all checks complete", 1.0), (1, "1 passed, 1 failed", 0.0), (2, "1 passed", 0.0), (5, "no tests ran", 0.0)])
async def test_eval_uses_pytest_exit_status(monkeypatch, exit_code: int, output: str, score: float) -> None:
    """Success is based on pytest's exit code, not text that may be truncated."""
    execute = Mock(return_value=SimpleNamespace(output=output, exit_code=exit_code))
    sandbox = SimpleNamespace(workdir="/home/test workspace", upload_files=Mock(return_value=[SimpleNamespace(error=None)]), execute=execute)
    runtime = SimpleNamespace(agent=SimpleNamespace(ainvoke=AsyncMock()), sandbox=sandbox)

    @asynccontextmanager
    async def session(**kwargs):
        yield runtime

    monkeypatch.setattr(exec_scorer, "agent_runtime_session", session)
    result = await exec_scorer.score_by_execution(None, {"id": "check", "issue": "task", "test_file": "/home/test workspace/hidden.py", "test_code": "assert True"})
    assert result["score"] == score
    command = execute.call_args.args[0]
    assert "|" not in command and "tail" not in command
    assert "'/home/test workspace/hidden.py'" in command


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_result", [[], [SimpleNamespace(error="permission_denied")]])
async def test_eval_aborts_when_hidden_test_upload_fails(monkeypatch, upload_result: list) -> None:
    """Do not award success when the hidden test never reached the sandbox."""
    execute = Mock()
    sandbox = SimpleNamespace(upload_files=Mock(return_value=upload_result), execute=execute)
    runtime = SimpleNamespace(agent=SimpleNamespace(ainvoke=AsyncMock()), sandbox=sandbox)

    @asynccontextmanager
    async def session(**kwargs):
        yield runtime

    monkeypatch.setattr(exec_scorer, "agent_runtime_session", session)
    result = await exec_scorer.score_by_execution(None, {"id": "check", "issue": "task", "test_file": "/hidden.py", "test_code": "assert True"})
    assert result["score"] == 0.0
    execute.assert_not_called()
