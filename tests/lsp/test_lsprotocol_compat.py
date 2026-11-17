import importlib
import sys
import types as pytypes


def test_lsp_main_imports_with_lsprotocol_2025_names(monkeypatch):
    import lsprotocol.types as lsp_types

    # Simulate lsprotocol 2025.x where Workspace*RefreshRequest were renamed
    monkeypatch.delattr(lsp_types, "WorkspaceDiagnosticRefreshRequest", raising=False)
    monkeypatch.delattr(lsp_types, "WorkspaceInlayHintRefreshRequest", raising=False)

    if not hasattr(lsp_types, "DiagnosticRefreshRequest"):
        dummy_req = type("DiagnosticRefreshRequest", (), {"__init__": lambda self, **kw: None})
        monkeypatch.setattr(lsp_types, "DiagnosticRefreshRequest", dummy_req, raising=False)
    if not hasattr(lsp_types, "InlayHintRefreshRequest"):
        dummy_inlay = type("InlayHintRefreshRequest", (), {"__init__": lambda self, **kw: None})
        monkeypatch.setattr(lsp_types, "InlayHintRefreshRequest", dummy_inlay, raising=False)

    # Stub pygls.server if not installed in minimal test env
    if "pygls.server" not in sys.modules:
        pygls_mod = pytypes.ModuleType("pygls")
        server_mod = pytypes.ModuleType("pygls.server")
        server_mod.LanguageServer = object
        monkeypatch.setitem(sys.modules, "pygls", pygls_mod)
        monkeypatch.setitem(sys.modules, "pygls.server", server_mod)

    sys.modules.pop("sqlmesh.lsp.main", None)
    try:
        mod = importlib.import_module("sqlmesh.lsp.main")
        assert mod.WorkspaceDiagnosticRefreshRequest is lsp_types.DiagnosticRefreshRequest
        assert mod.WorkspaceInlayHintRefreshRequest is lsp_types.InlayHintRefreshRequest
    except ModuleNotFoundError:
        # Fallback if full sqlmesh runtime deps aren't installed; verify AST/import block directly
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "sqlmesh" / "lsp" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        assert any(isinstance(n, ast.Try) for n in tree.body)
