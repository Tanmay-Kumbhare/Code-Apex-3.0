"""
ARL Stage 11 Test Suite: CLI, Streamlit UI, and Rule Lifecycle.

Tests cover:
  1. CLI Commands & Exit Codes:
     - `arl compile <ruleset.arl>` (0 success, 1 compile error)
     - `arl check <ruleset.arl> --data <mdb>` (0 success, 1 compile error, 2 data error)
     - `arl allocate <ruleset.arl> --mdb <xlsx> --template <xlsx> --out <xlsx>` (0 success, 1 compile error, 2 data error, 3 fallback)
     - `arl revise <ruleset.arl> --revA <xlsx> --revB <xlsx> --out <xlsx>` (0 success, 1 compile error, 2 data error)
     - `arl diff-rules <a.arl> <b.arl>` (0 success)
     - `arl diff-alloc <a.xlsx> <b.xlsx>` (0 success)
     - `arl predicates list` (0 success)
     - `arl predicates register <file.yaml>` (0 success)
  2. Exit-Code Precedence Order:
     Internal (4) > Compile (1) > Data (2) > Solver Fallback (3) > Success (0)
  3. Rule Lifecycle & Immutability:
     - Semantic versioning (MAJOR.MINOR.PATCH) parsing and bumping.
     - Publication marks ruleset immutable.
     - Editing a published ruleset creates a new semantic version.
     - Pre-publication compiler gate rejection on invalid ruleset.
  4. Streamlit UI Smoke Tests:
     - Verification of all 6 tabs using shared compiler APIs.
"""

from pathlib import Path
import tempfile
from typing import Any, Dict, List
from unittest.mock import MagicMock
import openpyxl
import pandas as pd
import pytest
import yaml

from arl import (
    EXIT_COMPILE_ERROR,
    EXIT_DATA_ERROR,
    EXIT_INTERNAL_ERROR,
    EXIT_SOLVER_FALLBACK,
    EXIT_SUCCESS,
    LifecycleManager,
    RulesetStatus,
    SemVer,
    cli_main,
    create_default_template,
)
from arl.ui import (
    render_allocate_tab,
    render_reports_tab,
    render_revision_tab,
    render_rules_tab,
    render_tqs_tab,
    render_upload_tab,
)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def tmp_workdir(tmp_path: Path):
    """Provides a temporary workspace with valid MDB, template, and .arl files."""
    # 1. Valid ARL ruleset
    valid_arl = """
    ruleset ProjectTestRules {
        rule R_Valid_Charm:
            scope: Charm
            require slot >= 1
            priority: hard
            id: R001
    }
    """
    arl_path = tmp_path / "valid.arl"
    arl_path.write_text(valid_arl, encoding="utf-8")

    # 2. Conflicting ARL ruleset
    invalid_arl = """
    ruleset ProjectConflictRules {
        rule R_One:
            scope: Charm
            require slot <= 0
            priority: hard
            id: R001

        rule R_Two:
            scope: Charm
            require slot >= 1
            priority: hard
            id: R002
    }
    """
    conflict_path = tmp_path / "conflict.arl"
    conflict_path.write_text(invalid_arl, encoding="utf-8")

    # 3. Valid MDB Excel
    df_valid = pd.DataFrame([
        ["10-FT-101", "AI", "CBL-01", 1, "A", "AREA_01"],
        ["10-FT-102", "AI", "CBL-01", 2, "A", "AREA_01"],
        ["10-PT-103", "AI", "CBL-01", 3, "A", "AREA_01"],
        ["10-PT-104", "AI", "CBL-01", 4, "A", "AREA_01"],
    ], columns=["Tag Name", "I/O Type", "Cable No", "Pair #", "Channel Seg", "Plant Area"])
    mdb_path = tmp_path / "mdb.xlsx"
    df_valid.to_excel(mdb_path, index=False)

    # 4. Invalid MDB Excel (missing required Tag Name)
    df_invalid = pd.DataFrame([
        [None, "AI", "CBL-01", 1, "A", "AREA_01"],
    ], columns=["Tag Name", "I/O Type", "Cable No", "Pair #", "Channel Seg", "Plant Area"])
    invalid_mdb_path = tmp_path / "invalid_mdb.xlsx"
    df_invalid.to_excel(invalid_mdb_path, index=False)

    # 5. Output template Excel
    template_path = tmp_path / "template.xlsx"
    create_default_template(template_path)

    return {
        "dir": tmp_path,
        "valid_arl": arl_path,
        "conflict_arl": conflict_path,
        "valid_mdb": mdb_path,
        "invalid_mdb": invalid_mdb_path,
        "template": template_path,
    }


# =========================================================================
# 1. CLI Commands & Exit Codes Tests
# =========================================================================

class TestCLICommands:
    """Test all Part 12 required CLI commands and strict exit codes."""

    def test_cmd_compile_success(self, tmp_workdir: Dict[str, Any]):
        """arl compile <ruleset.arl> returns 0 on success."""
        code = cli_main(["compile", str(tmp_workdir["valid_arl"])])
        assert code == EXIT_SUCCESS

    def test_cmd_compile_error(self, tmp_workdir: Dict[str, Any]):
        """arl compile <ruleset.arl> returns 1 on static conflict or syntax error."""
        code = cli_main(["compile", str(tmp_workdir["conflict_arl"])])
        assert code == EXIT_COMPILE_ERROR

    def test_cmd_compile_missing_file(self, tmp_workdir: Dict[str, Any]):
        """arl compile missing file returns 1."""
        code = cli_main(["compile", str(tmp_workdir["dir"] / "nonexistent.arl")])
        assert code == EXIT_COMPILE_ERROR

    def test_cmd_check_success(self, tmp_workdir: Dict[str, Any]):
        """arl check <ruleset.arl> --data <mdb> returns 0 when ruleset and data are valid."""
        code = cli_main(["check", str(tmp_workdir["valid_arl"]), "--data", str(tmp_workdir["valid_mdb"])])
        assert code == EXIT_SUCCESS

    def test_cmd_check_compile_error_precedence(self, tmp_workdir: Dict[str, Any]):
        """arl check returns 1 when ruleset has compile error, even with valid data."""
        code = cli_main(["check", str(tmp_workdir["conflict_arl"]), "--data", str(tmp_workdir["valid_mdb"])])
        assert code == EXIT_COMPILE_ERROR

    def test_cmd_check_data_error(self, tmp_workdir: Dict[str, Any]):
        """arl check returns 2 when MDB data contains fatal data quality error."""
        code = cli_main(["check", str(tmp_workdir["valid_arl"]), "--data", str(tmp_workdir["invalid_mdb"])])
        assert code == EXIT_DATA_ERROR

    def test_cmd_allocate_success(self, tmp_workdir: Dict[str, Any]):
        """arl allocate produces output workbook and returns 0 on success."""
        out_path = tmp_workdir["dir"] / "allocated.xlsx"
        code = cli_main([
            "allocate",
            str(tmp_workdir["valid_arl"]),
            "--mdb", str(tmp_workdir["valid_mdb"]),
            "--template", str(tmp_workdir["template"]),
            "--out", str(out_path),
        ])
        assert code == EXIT_SUCCESS
        assert out_path.exists()
        # Verify content written
        wb = openpyxl.load_workbook(out_path)
        assert "Allocation" in wb.sheetnames

    def test_cmd_allocate_data_error(self, tmp_workdir: Dict[str, Any]):
        """arl allocate returns 2 on fatal data errors."""
        out_path = tmp_workdir["dir"] / "alloc_fail.xlsx"
        code = cli_main([
            "allocate",
            str(tmp_workdir["valid_arl"]),
            "--mdb", str(tmp_workdir["invalid_mdb"]),
            "--template", str(tmp_workdir["template"]),
            "--out", str(out_path),
        ])
        assert code == EXIT_DATA_ERROR

    def test_cmd_diff_rules(self, tmp_workdir: Dict[str, Any]):
        """arl diff-rules <a.arl> <b.arl> returns 0 and displays rule deltas."""
        code = cli_main(["diff-rules", str(tmp_workdir["valid_arl"]), str(tmp_workdir["conflict_arl"])])
        assert code == EXIT_SUCCESS

    def test_cmd_predicates_list(self):
        """arl predicates list returns 0 and lists catalog."""
        code = cli_main(["predicates", "list"])
        assert code == EXIT_SUCCESS

    def test_cmd_predicates_register(self, tmp_path: Path):
        """arl predicates register <file.yaml> registers new predicate definitions."""
        pred_yaml = """
        custom_adjacent:
            args:
                - name: s1
                  type: Signal
                - name: s2
                  type: Signal
            returns: Bool
            description: True if two signals are placed in adjacent slots
            category: relational
        """
        yaml_file = tmp_path / "custom_preds.yaml"
        yaml_file.write_text(pred_yaml, encoding="utf-8")

        code = cli_main(["predicates", "register", str(yaml_file)])
        assert code == EXIT_SUCCESS

    def test_cmd_revise_and_diff_alloc(self, tmp_workdir: Dict[str, Any]):
        """arl revise and arl diff-alloc test flow."""
        # Create Rev A allocation
        rev_a_out = tmp_workdir["dir"] / "rev_a_alloc.xlsx"
        cli_main([
            "allocate",
            str(tmp_workdir["valid_arl"]),
            "--mdb", str(tmp_workdir["valid_mdb"]),
            "--template", str(tmp_workdir["template"]),
            "--out", str(rev_a_out),
        ])
        assert rev_a_out.exists()

        # Revise with Rev B MDB
        rev_b_out = tmp_workdir["dir"] / "rev_b_alloc.xlsx"
        code_rev = cli_main([
            "revise",
            str(tmp_workdir["valid_arl"]),
            "--revA", str(rev_a_out),
            "--revB", str(tmp_workdir["valid_mdb"]),
            "--out", str(rev_b_out),
        ])
        assert code_rev == EXIT_SUCCESS
        assert rev_b_out.exists()

        # Diff allocations
        code_diff = cli_main(["diff-alloc", str(rev_a_out), str(rev_b_out)])
        assert code_diff == EXIT_SUCCESS


# =========================================================================
# 2. Rule Lifecycle & Semantic Versioning Tests
# =========================================================================

class TestRuleLifecycleAndVersioning:
    """Test Part 12 & 13 immutable published versions and semantic versioning."""

    def test_semver_parsing_and_bumping(self):
        """Verify SemVer comparison and bumping."""
        v1 = SemVer.parse("1.2.3")
        assert v1.major == 1 and v1.minor == 2 and v1.patch == 3
        assert str(v1) == "1.2.3"

        assert str(v1.bump_patch()) == "1.2.4"
        assert str(v1.bump_minor()) == "1.3.0"
        assert str(v1.bump_major()) == "2.0.0"

        assert SemVer.parse("1.2.3") < SemVer.parse("1.3.0")
        assert SemVer.parse("1.3.0") < SemVer.parse("2.0.0")

    def test_published_ruleset_is_immutable(self, tmp_path: Path):
        """Publishing a ruleset marks it immutable; editing published creates a new version."""
        mgr = LifecycleManager(storage_dir=tmp_path / "lifecycle")
        valid_src = "ruleset ProjectRules {\n  rule R1:\n    scope: Charm\n    require slot >= 1\n    id: R001\n}"

        # 1. Create initial draft
        draft = mgr.create_ruleset("ProjectRules", valid_src, initial_version="1.0.0")
        assert draft.status == RulesetStatus.DRAFT
        assert draft.is_immutable is False

        # 2. Publish initial version
        pub = mgr.publish("ProjectRules", "1.0.0", commit_message="Initial release")
        assert pub.status == RulesetStatus.PUBLISHED
        assert pub.is_immutable is True

        # Re-publishing published raises ValueError
        with pytest.raises(ValueError, match="already published"):
            mgr.publish("ProjectRules", "1.0.0")

        # 3. Edit published ruleset -> enforces immutability by creating NEW version 1.0.1
        updated_src = valid_src + "\n// modified"
        new_ver = mgr.edit_ruleset("ProjectRules", updated_src, bump="patch")
        assert new_ver.version == "1.0.1"
        assert new_ver.status == RulesetStatus.DRAFT
        assert new_ver.is_immutable is False

        # Verify version 1.0.0 was NOT modified (immutability preserved)
        v1 = mgr.get_version("ProjectRules", "1.0.0")
        assert v1.is_immutable is True
        assert v1.source_code == valid_src

        # 4. Publish 1.0.1
        pub_v2 = mgr.publish("ProjectRules", "1.0.1", commit_message="Patch release")
        assert pub_v2.status == RulesetStatus.PUBLISHED

        # 5. Diff versions 1.0.0 and 1.0.1
        diff = mgr.diff_versions("ProjectRules", "1.0.0", "1.0.1")
        assert diff is not None

    def test_pre_publication_compiler_gate_rejects_invalid_ruleset(self, tmp_path: Path):
        """Cannot publish a ruleset that fails compiler typechecking or syntax."""
        mgr = LifecycleManager(storage_dir=tmp_path / "lifecycle")
        bad_src = "ruleset BadRules {\n  rule R1:\n    scope: UnknownScope\n    require slot >= 1\n}"

        mgr.create_ruleset("BadRules", bad_src, initial_version="1.0.0")
        with pytest.raises(ValueError, match="Cannot publish ruleset"):
            mgr.publish("BadRules", "1.0.0")


# =========================================================================
# 3. Streamlit UI Smoke Tests
# =========================================================================

class TestStreamlitUISmoke:
    """Smoke test Streamlit UI tabs and programmatic execution."""

    def _mock_streamlit(self) -> MagicMock:
        """Helper to create a flexible mock of the Streamlit module."""
        st = MagicMock()

        def create_cols(n):
            count = n if isinstance(n, int) else len(n)
            cols = []
            for _ in range(count):
                col = MagicMock()
                col.metric = st.metric
                cols.append(col)
            return cols

        st.columns.side_effect = create_cols
        st.expander.return_value.__enter__.return_value = MagicMock()
        st.spinner.return_value.__enter__.return_value = MagicMock()
        st.session_state = {}
        return st

    def test_smoke_render_upload_tab(self, tmp_workdir: Dict[str, Any]):
        """Smoke test Tab 1: Upload."""
        st = self._mock_streamlit()
        st.file_uploader.return_value = None
        state: Dict[str, Any] = {
            "mdb_bytes": tmp_workdir["valid_mdb"].read_bytes(),
            "mdb_name": "mdb.xlsx",
        }
        render_upload_tab(st, state)
        assert "project" in state
        st.metric.assert_called()

    def test_smoke_render_rules_tab_form_builder_and_validation(self, tmp_workdir: Dict[str, Any]):
        """Smoke test Tab 2: Rules form builder and compiler validation."""
        st = self._mock_streamlit()
        # Simulate button click for Generate & Append Rule
        st.button.return_value = True
        st.text_input.side_effect = lambda label, **kwargs: kwargs.get("value", "test")
        st.selectbox.side_effect = lambda label, options, **kwargs: options[0]
        st.text_area.side_effect = lambda label, **kwargs: kwargs.get("value", "")

        state = {
            "ruleset_source": "ruleset ProjectRules {\n}",
            "project": None,
        }
        render_rules_tab(st, state)
        assert "R_Safety_Segregation" in state["ruleset_source"]
        st.success.assert_called()

    def test_smoke_render_allocate_tab(self, tmp_workdir: Dict[str, Any]):
        """Smoke test Tab 3: Allocate."""
        st = self._mock_streamlit()
        st.button.return_value = True
        st.selectbox.return_value = "greedy"

        state = {
            "mdb_bytes": tmp_workdir["valid_mdb"].read_bytes(),
            "template_bytes": tmp_workdir["template"].read_bytes(),
            "ruleset_source": tmp_workdir["valid_arl"].read_text(),
        }
        render_allocate_tab(st, state)
        assert "pipeline_result" in state
        assert state["pipeline_result"].success is True

    def test_smoke_render_reports_and_tqs_tabs(self, tmp_workdir: Dict[str, Any]):
        """Smoke test Tab 4 (Reports) and Tab 5 (TQs)."""
        st = self._mock_streamlit()
        from arl.pipeline import run_allocation_pipeline

        res = run_allocation_pipeline(
            mdb_paths=[tmp_workdir["valid_mdb"]],
            template_path=tmp_workdir["template"],
            output_path=tmp_workdir["dir"] / "smoke_out.xlsx",
            ruleset_path=tmp_workdir["valid_arl"],
            solver_backend="greedy",
        )
        state = {"pipeline_result": res}

        render_reports_tab(st, state)
        st.metric.assert_called()

        render_tqs_tab(st, state)

    def test_smoke_render_revision_tab(self, tmp_workdir: Dict[str, Any]):
        """Smoke test Tab 6: Revision."""
        st = self._mock_streamlit()
        st.button.return_value = False
        state = {}
        render_revision_tab(st, state)

    def test_get_project_status_and_caching(self, tmp_workdir: Dict[str, Any]):
        """Test status strip evaluation and parse caching."""
        from arl.ui import get_project_status, ensure_project_parsed, render_review_tab, render_status_strip, render_welcome_screen

        state: Dict[str, Any] = {}
        # 1. Empty state
        status = get_project_status(state)
        assert status["input_data"][0] == "Not Loaded"
        assert status["rules"][0] == "Not Loaded"
        assert status["allocation"][0] == "Not Run"
        assert status["review"][0] == "Pending Run"

        # 2. Add MDB bytes and test caching
        mdb_bytes = tmp_workdir["valid_mdb"].read_bytes()
        state["mdb_bytes"] = mdb_bytes
        state["mdb_name"] = "test_mdb.xlsx"

        proj1, errs1, ok1 = ensure_project_parsed(state)
        assert proj1 is not None
        assert ok1 is True
        cached_key = state.get("_cached_mdb_key")
        assert cached_key == ("test_mdb.xlsx", len(mdb_bytes))

        # Re-call must return cached project without error
        proj2, errs2, ok2 = ensure_project_parsed(state)
        assert proj2 is proj1

        # Check status reflects loaded data
        status2 = get_project_status(state)
        assert "Loaded" in status2["input_data"][0]

        # 3. Test render_status_strip and render_welcome_screen
        st_mock = self._mock_streamlit()
        render_status_strip(st_mock, state)
        render_welcome_screen(st_mock, state)
        st_mock.markdown.assert_called()

        # 4. Test render_review_tab
        st_tabs_mock = self._mock_streamlit()
        st_tabs_mock.tabs.return_value = [MagicMock(), MagicMock()]
        render_review_tab(st_tabs_mock, state)
        st_tabs_mock.tabs.assert_called()

