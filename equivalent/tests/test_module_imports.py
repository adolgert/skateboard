"""Runtime dependencies must form a DAG, including imports inside functions.

Package-layer tests alone miss cycles between two modules in one package.
This scans first-party Python source without importing it; type-only imports
are reported separately and do not impose runtime initialization constraints.
Docker-copied modules and reflective imports are outside this source graph.
"""
import ast
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def _imports(source, package, modules):
    edges = {"runtime": set(), "type_only": set()}

    class Imports(ast.NodeVisitor):
        type_only = False

        def visit_If(self, node):
            guard = node.test
            type_guard = (
                isinstance(guard, ast.Name) and guard.id == "TYPE_CHECKING"
                or isinstance(guard, ast.Attribute) and guard.attr == "TYPE_CHECKING"
                and isinstance(guard.value, ast.Name) and guard.value.id == "typing"
            )
            if not type_guard:
                self.generic_visit(node)
                return
            before = self.type_only
            self.type_only = True
            for child in node.body:
                self.visit(child)
            self.type_only = before
            for child in node.orelse:
                self.visit(child)

        def add(self, name):
            if name in modules:
                edges["type_only" if self.type_only else "runtime"].add(name)

        def visit_Import(self, node):
            for alias in node.names:
                self.add(alias.name)

        def visit_ImportFrom(self, node):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[:len(parts) - node.level + 1])
                if node.module:
                    base += "." + node.module
            else:
                base = node.module or ""
            for alias in node.names:
                child = f"{base}.{alias.name}"
                self.add(child if child in modules else base)

    Imports().visit(ast.parse(source))
    return edges


def _cycles(graph):
    """Strongly connected components, including self-imports."""
    indices, low = {}, {}
    stack, active, cycles = [], set(), []

    def visit(name):
        indices[name] = low[name] = len(indices)
        stack.append(name)
        active.add(name)
        for dependency in sorted(graph[name]):
            if dependency not in indices:
                visit(dependency)
                low[name] = min(low[name], low[dependency])
            elif dependency in active:
                low[name] = min(low[name], indices[dependency])
        if low[name] != indices[name]:
            return
        component = []
        while True:
            item = stack.pop()
            active.remove(item)
            component.append(item)
            if item == name:
                break
        if len(component) > 1 or name in graph[name]:
            cycles.append(tuple(sorted(component)))

    for name in sorted(graph):
        if name not in indices:
            visit(name)
    return sorted(cycles)


def test_runtime_modules_have_no_cycles_including_lazy_imports():
    modules = {}
    for directory in ("equivalent", "services"):
        for path in (REPOSITORY / directory).rglob("*.py"):
            relative = path.relative_to(REPOSITORY)
            if "tests" in relative.parts:
                continue
            parts = relative.with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            modules[".".join(parts)] = path
    graph = {}
    for name, path in modules.items():
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        graph[name] = _imports(path.read_text(), package, modules)["runtime"]
    assert _cycles(graph) == [], "runtime module cycles (including function-local imports)"


def test_scanner_resolves_relative_and_from_package_imports():
    modules = {"sample", "sample.one", "sample.two", "sample.nested.three"}
    found = _imports(
        "from .. import one\nfrom sample import two\nfrom .three import function\n",
        "sample.nested", modules,
    )
    assert found["runtime"] == {"sample.one", "sample.two", "sample.nested.three"}


def test_scanner_includes_lazy_imports_but_separates_type_annotations():
    source = """
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sample.context import Context
else:
    import sample.runtime
def use():
    from sample.phase import phase_for
"""
    found = _imports(source, "sample", {"sample.context", "sample.runtime", "sample.phase"})
    assert found == {
        "runtime": {"sample.runtime", "sample.phase"},
        "type_only": {"sample.context"},
    }


def test_scanner_finds_a_lazy_cycle_longer_than_two_modules():
    modules = {"sample.a", "sample.b", "sample.c", "sample.consumer"}
    sources = {
        "sample.a": "def use():\n    from .b import B\n",
        "sample.b": "from .c import C\n",
        "sample.c": "from .a import A\n",
        "sample.consumer": "from .a import A\n",
    }
    graph = {name: _imports(source, "sample", modules)["runtime"] for name, source in sources.items()}
    assert _cycles(graph) == [("sample.a", "sample.b", "sample.c")]


def test_type_annotation_back_reference_does_not_create_a_runtime_cycle():
    modules = {"sample.context", "sample.phase"}
    sources = {
        "sample.context": "from .phase import phase_for",
        "sample.phase": "import typing\nif typing.TYPE_CHECKING:\n    from .context import Context",
    }
    graph = {name: _imports(source, "sample", modules)["runtime"] for name, source in sources.items()}
    assert _cycles(graph) == []
