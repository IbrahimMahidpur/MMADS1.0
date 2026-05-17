import os
import tempfile
import json
from pathlib import Path

# Import the graph module functions
from multimodal_ds.graph import _executor_node, _reviewer_node

# Stub CodeExecutionAgent to avoid LLM calls
class DummyCodeExecutionAgent:
    def __init__(self, session_id: str = "default"):
        self.session_id = session_id

    def execute(self, task_description: str, data_context: str = "", file_paths=None, max_retries: int = 2):
        return {
            "success":       True,
            "code":          "# dummy code",
            "output":        "Execution successful\n=== FINDINGS ===\nAccuracy: 0.85",
            "files_created": ["dummy_output.txt"],
            "error":         "",
            "retries_used":  0,
        }


# Stub EvaluationAgent — matches the real interface after Phase 14 rewrite
class DummyEvalReport:
    def __init__(self, task_results):
        self._task_results = task_results

    def to_dict(self) -> dict:
        return {
            "session_id":            "test_session",
            "task_count":            len(self._task_results),
            "flagged_count":         0,
            "pass_count":            len(self._task_results),
            "overall_session_score": 7.0,
            "session_verdict":       "PASS",
            "evaluations":           [],
            # Include output previews so tests can assert on content
            "task_outputs":  [tr.get("output_preview", "") for tr in self._task_results],
            "task_results":  [tr.get("output_preview", "") for tr in self._task_results],
        }


class DummyEvaluationAgent:
    def __init__(self, session_id: str = "default", **kwargs):
        self.session_id = session_id

    def evaluate_task_results(
        self,
        task_results,
        data_context: str = "",
        stat_report=None,
    ) -> DummyEvalReport:
        return DummyEvalReport(task_results)


# Monkey-patch both agents used inside the graph module
import multimodal_ds.graph as graph
graph.CodeExecutionAgent  = DummyCodeExecutionAgent
graph.EvaluationAgent     = DummyEvaluationAgent


def test_executor_node_returns_files_created():
    state = {
        "analysis_tasks":     [{"name": "test_task", "description": "test description", "type": "eda"}],
        "current_step":       0,
        "session_id":         "test_session",
        "uploaded_files":     [],
        "tabular_summaries":  [],
        "parsed_documents":   [],
        "image_embeddings":   [],
        "full_code_outputs":  [],
        "errors":             [],
        "visualizations":     [],
        "saved_artifacts":    [],
        "current_step_files": [],
        "files_created":      [],
        "current_step_success": False,
        "step_file_map":      {},
        "_last_files_created": [],
        "_last_success":      False,
    }

    result = _executor_node(state)

    assert result["current_step"] == 1, "Step should advance from 0 to 1"
    assert result["current_step_files"], "current_step_files should be non-empty"
    assert "dummy_output.txt" in result["current_step_files"]
    # Verify full_code_outputs uses 'output' key (Phase 18 fix)
    assert result["full_code_outputs"] == ["Execution successful\n=== FINDINGS ===\nAccuracy: 0.85"]
    # Verify step_file_map uses string keys (Phase 18 fix)
    assert "0" in result["step_file_map"], "step_file_map must use string keys"


def test_reviewer_node_includes_output_preview():
    state = {
        "analysis_tasks":     [{"name": "test_task", "description": "test description", "type": "eda"}],
        "full_code_outputs":  ["Mean: 5, Std: 2, Accuracy: 0.85"],
        "errors":             [],
        "visualizations":     [],
        "saved_artifacts":    [],
        "_last_files_created": ["dummy_output.txt"],
        "session_id":         "test_session",
        "tabular_summaries":  [],
        "statistical_report": {},
        "step_file_map":      {"0": ["dummy_output.txt"]},
    }

    result = _reviewer_node(state)
    eval_report = result.get("eval_report")

    assert eval_report is not None, "eval_report must be present in result"
    # After Phase 14, eval_report is a dict (from EvalReport.to_dict())
    assert isinstance(eval_report, dict), "eval_report should be a dict after to_dict()"
    assert "task_outputs" in eval_report, "eval_report dict must have task_outputs key"
    assert "Mean: 5" in eval_report["task_outputs"][0], \
        f"Expected 'Mean: 5' in output preview, got: {eval_report['task_outputs'][0]!r}"


def test_executor_node_file_copy_gated_by_task_type():
    """Executor should only pass file_paths for data-access task types (Phase 19 fix)."""
    calls = []
    original_execute = DummyCodeExecutionAgent.execute

    def patched_execute(self, task_description, data_context="", file_paths=None, max_retries=2):
        calls.append({"file_paths": file_paths})
        return original_execute(self, task_description, data_context, file_paths, max_retries)

    DummyCodeExecutionAgent.execute = patched_execute

    try:
        # Visualization task — should NOT receive file_paths
        state_viz = {
            "analysis_tasks":     [{"name": "viz", "description": "plot results", "type": "visualization"}],
            "current_step":       0,
            "session_id":         "test_session",
            "uploaded_files":     ["/data/large_file.parquet"],
            "tabular_summaries":  [],
            "parsed_documents":   [],
            "image_embeddings":   [],
            "full_code_outputs":  [],
            "errors":             [],
            "visualizations":     [],
            "saved_artifacts":    [],
            "current_step_files": [],
            "files_created":      [],
            "current_step_success": False,
            "step_file_map":      {},
            "_last_files_created": [],
            "_last_success":      False,
        }
        _executor_node(state_viz)
        assert calls[-1]["file_paths"] == [], \
            f"visualization task should not receive file_paths, got {calls[-1]['file_paths']}"

        # EDA task — SHOULD receive file_paths
        state_eda = dict(state_viz)
        state_eda["analysis_tasks"] = [{"name": "eda", "description": "explore data", "type": "eda"}]
        _executor_node(state_eda)
        assert calls[-1]["file_paths"] == ["/data/large_file.parquet"], \
            f"eda task should receive file_paths, got {calls[-1]['file_paths']}"

    finally:
        DummyCodeExecutionAgent.execute = original_execute