"""Which parts of the package are allowed to know about which other parts.

A component judges a tree and says pass or fail. It has no business
knowing how a tree got into a git repository, which is what the gateway
does, and an import that says otherwise makes the check impossible to
run or to test without standing up the gateway around it. Reading the
tree lives below both of them, and that is what a component imports.
"""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent

# Every top-level name under the package: a directory that is a package,
# plus a module that stands on its own. The tests themselves are left
# out; they import everything by design.
TOP_LEVEL = sorted(
    {p.name for p in PACKAGE.iterdir() if (p / "__init__.py").exists() and p.name != "tests"}
    | {p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__"}
)


def _imported_top_level(source: str, own_package: str) -> set[str]:
    """The package's own top-level names that one module's source imports."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # A relative import stays inside the module's own package.
            names = [f"{own_package}." if node.level else (node.module or "")]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] == "equivalent" and len(parts) > 1 and parts[1] in TOP_LEVEL:
                found.add(parts[1])
    return found - {own_package}


def _adjacency() -> dict[str, set[str]]:
    """For each top-level name, the siblings its modules import."""
    edges = {name: set() for name in TOP_LEVEL}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] == "tests":
            continue
        own = relative.parts[0] if len(relative.parts) > 1 else relative.stem
        if own in edges:
            edges[own] |= _imported_top_level(path.read_text(), own)
    return edges


def test_no_component_imports_the_gateway():
    edges = _adjacency()

    offenders = sorted(
        path.relative_to(PACKAGE.parent).as_posix()
        for path in sorted((PACKAGE / "components").rglob("*.py"))
        if "gateway" in _imported_top_level(path.read_text(), "components")
    )

    assert offenders == [], (
        f"these components import the gateway: {offenders}. "
        f"Reading a tree is equivalent.tree, below both. "
        f"The whole picture, package by package: "
        + "; ".join(f"{name} -> {sorted(edges[name])}" for name in sorted(edges))
    )
