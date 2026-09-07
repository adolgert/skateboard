"""Which parts of the package are allowed to know about which other parts.

A component judges a tree and says pass or fail. It has no business
knowing how a tree got into a git repository, which is what the gateway
does, and an import that says otherwise makes the check impossible to
run or to test without standing up the gateway around it. Reading the
tree lives below both of them, and that is what a component imports.

The same reasoning runs the other way for the command line. A person
reads a ledger on a host where no gateway is installed, so nothing the
CLI reaches may pull the gateway's HTTP surface, its locks, or its
backend clients in behind it. What the CLI and the gateway genuinely
share -- what a region is, what its current tree and allow-list are,
which claims a later claim rests on, the precondition table -- is domain
policy, and it lives below both.

The edges below are the whole of what is allowed. A package may import
fewer than its row lists; importing anything the row does not name is
the failure.
"""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
REPOSITORY = PACKAGE.parent

# Every top-level name under the package: a directory that is a package,
# plus a module that stands on its own. The tests themselves are left
# out; they import everything by design.
TOP_LEVEL = sorted(
    {p.name for p in PACKAGE.iterdir() if (p / "__init__.py").exists() and p.name != "tests"}
    | {p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__"}
)

# What each part of the package may import, in layers. Nothing below the
# line reaches upward:
#
#   capture, analyzers, client   know about nothing else
#   ledger                       the claims and the table over them
#   manifest, reference,         what a code and a region are described by
#   strategy, tree
#   region                       one deployed region, read-only
#   components                   one check over a tree
#   promote                      the end of an onboarding session
#   cli, gateway                 the two front doors
ALLOWED = {
    "analyzers": set(),
    "capture": set(),
    "client": set(),
    "ledger": {"capture"},
    "manifest": {"capture", "ledger"},
    "reference": {"ledger"},
    "strategy": {"ledger"},
    "tree": {"ledger", "manifest"},
    "region": {"ledger", "manifest", "reference", "strategy", "tree"},
    "components": {"capture", "ledger", "manifest", "reference", "strategy", "tree"},
    # Promotion reads claims and copies files; the checks that produced
    # those claims are not among what it needs, and reaching for one
    # would put a component's errors in front of a person running a
    # command.
    "promote": {"ledger", "manifest", "region", "strategy", "tree"},
    "cli": {"ledger", "promote", "region", "strategy"},
    "gateway": {"components", "ledger", "region", "strategy", "tree"},
}

# What the deployment scripts may reach for. They drive the deployment
# from outside it: a client that speaks to the gateway over HTTP, and the
# names a code directory is laid out with. The CLI's own modules are not
# among them -- a script that reached into `equivalent.cli` would be
# depending on how a command is implemented rather than on what it does.
DEPLOY_ALLOWED = {"client", "manifest"}


def _package_of(path: Path) -> tuple[str, ...]:
    """The dotted package one module lives in, as Python names it."""
    return ("equivalent", *path.relative_to(PACKAGE).parts[:-1])


def _absolute(node: ast.ImportFrom, package: tuple[str, ...]) -> str:
    """One `from ... import` as the module name Python would resolve it to.

    A relative import is resolved against the module's own package, one
    dot per level, the way the interpreter resolves it. Reading every
    relative import as the module's own package instead would hide the
    import that matters most here: a component reaching up to the gateway
    with `from ..gateway import ...` would look like no import at all.
    """
    if not node.level:
        return node.module or ""
    base = package[: len(package) - node.level + 1]
    return ".".join((*base, node.module) if node.module else base)


def _imported_top_level(source: str, package: tuple[str, ...]) -> set[str]:
    """The package's own top-level names that one module's source imports."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, package)
            names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] == "equivalent" and len(parts) > 1 and parts[1] in TOP_LEVEL:
                found.add(parts[1])
    return found


def _adjacency() -> dict[str, set[str]]:
    """For each top-level name, the siblings its modules import."""
    edges = {name: set() for name in TOP_LEVEL}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] == "tests":
            continue
        own = relative.parts[0] if len(relative.parts) > 1 else relative.stem
        if own in edges:
            edges[own] |= _imported_top_level(path.read_text(), _package_of(path)) - {own}
    return edges


def _picture(edges: dict[str, set[str]]) -> str:
    return "; ".join(f"{name} -> {sorted(edges[name])}" for name in sorted(edges))


def _deploy_imports() -> dict[str, set[str]]:
    """For each deployment script, what of the package it names."""
    found = {}
    for path in sorted((REPOSITORY / "deploy").glob("*.py")):
        names = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                raw = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                raw = [base, *(f"{base}.{alias.name}" for alias in node.names)]
            else:
                continue
            for name in raw:
                parts = name.split(".")
                if parts[0] == "equivalent" and len(parts) > 1:
                    names.add(".".join(parts[1:3]) if parts[1] == "cli" else parts[1])
        if names:
            found[path.name] = names
    return found


def test_every_package_imports_only_what_its_layer_allows():
    edges = _adjacency()

    assert set(edges) == set(ALLOWED), (
        f"a top-level name appeared or vanished; say what it may import. "
        f"Found {sorted(edges)}, expected {sorted(ALLOWED)}"
    )
    over_reaching = {
        name: sorted(edges[name] - ALLOWED[name])
        for name in sorted(edges)
        if edges[name] - ALLOWED[name]
    }

    assert over_reaching == {}, (
        f"these imports are outside what the layer allows: {over_reaching}. "
        f"The whole picture, package by package: {_picture(edges)}"
    )


def test_no_component_imports_the_gateway_or_a_deployed_region():
    # A component is handed a tree, the claims it rests on, and read-only
    # access to the sets it compares against, and answers a question
    # about them. Reaching for the gateway or for a region's own
    # configuration would make it unrunnable without a deployment around
    # it.
    edges = _adjacency()

    offenders = sorted(
        path.relative_to(PACKAGE.parent).as_posix()
        for path in sorted((PACKAGE / "components").rglob("*.py"))
        if {"gateway", "region", "cli"} & _imported_top_level(
            path.read_text(), _package_of(path),
        )
    )

    assert offenders == [], (
        f"these components import the gateway, a region, or the CLI: {offenders}. "
        f"Reading a tree is equivalent.tree, below all of them. "
        f"The whole picture, package by package: {_picture(edges)}"
    )


def test_the_command_line_works_without_the_gateway_installed():
    # A person reads a ledger on the host, where the gateway's HTTP
    # surface, its locks, and its backend clients are not wanted and need
    # not be installed.
    edges = _adjacency()

    reaching = sorted(name for name in ("cli", "promote", "region") if "gateway" in edges[name])

    assert reaching == [], (
        f"{reaching} import the gateway, which the host does not run. "
        f"The whole picture, package by package: {_picture(edges)}"
    )


def test_nothing_below_the_deployment_forms_an_import_cycle():
    # These are the layers every front door rests on. A cycle among them
    # means neither can be read, tested, or replaced on its own.
    edges = _adjacency()
    below = ("capture", "manifest", "ledger", "reference", "strategy", "tree")

    cycles = sorted(
        (a, b) for a in below for b in sorted(edges[a] & set(below)) if a in edges[b]
    )

    assert cycles == [], (
        f"these pairs import each other: {cycles}. "
        f"The whole picture, package by package: {_picture(edges)}"
    )


def test_the_deployment_scripts_reach_only_for_the_client_and_the_layout():
    over_reaching = {
        name: sorted(names - DEPLOY_ALLOWED)
        for name, names in sorted(_deploy_imports().items())
        if names - DEPLOY_ALLOWED
    }

    assert over_reaching == {}, (
        f"these deployment scripts import package internals: {over_reaching}; "
        f"they may name {sorted(DEPLOY_ALLOWED)}"
    )
