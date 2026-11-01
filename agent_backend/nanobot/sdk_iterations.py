"""Local, version-checked support for unlimited nanobot 0.4.0 iterations.

The SDK uses ``range(spec.max_iterations)``; zero otherwise runs no model
rounds. Adapt exactly that iterator and its follow-up check in a private
subclass. No installed source or module globals are changed. All tool,
cancellation, provider, transcript and completion behavior stays upstream.
"""
from __future__ import annotations

import ast
from importlib.metadata import version
import inspect
from itertools import count
import textwrap


class UnlimitedIterationsUnavailable(RuntimeError):
    """The pinned SDK no longer has the exact supported loop structure."""


def _iteration_indexes(limit: int):
    if type(limit) is not int or limit < 0:
        raise ValueError("invalid iteration limit")
    return count() if limit == 0 else range(limit)


def _same(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def unlimited_iterations_runner():
    """Return a private SDK runner with a true unbounded zero-mode iterator.

    Fail closed with a static worker error if an SDK upgrade changes either
    supported node. Positive compatibility limits use the untouched SDK runner
    in worker.py; no giant integer is substituted for an unlimited limit.
    """
    try:
        if version("nanobot-ai") != "0.4.0":
            raise ValueError("unsupported SDK")
        from nanobot.agent.runner import AgentRunner

        method = AgentRunner._run_core
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.AsyncFunctionDef):
            raise ValueError("unsupported runner")
        function = tree.body[0]
        if function.name != "_run_core" or function.decorator_list:
            raise ValueError("unsupported runner")
        expected_iterator = ast.parse("range(spec.max_iterations)", mode="eval").body
        expected_followup = ast.parse("iteration + 1 < spec.max_iterations", mode="eval").body
        loops, followups = [], []
        for node in ast.walk(function):
            if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                    and node.target.id == "iteration" and _same(node.iter, expected_iterator)):
                loops.append(node)
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "can_make_followup_request"
                    and _same(node.value, expected_followup)):
                followups.append(node)
        if len(loops) != 1 or len(followups) != 1:
            raise ValueError("unsupported loop structure")
        # Reject extra limit-dependent branches instead of silently retaining
        # an unknown finite guard from a future patched build of the SDK.
        limit_nodes = [node for node in ast.walk(function)
                       if isinstance(node, ast.Attribute) and node.attr == "max_iterations"
                       and isinstance(node.value, ast.Name) and node.value.id == "spec"]
        if len(limit_nodes) != 2:
            raise ValueError("unsupported limit structure")
        loops[0].iter = ast.copy_location(
            ast.parse("_aura_iteration_indexes(spec.max_iterations)", mode="eval").body,
            loops[0].iter)
        followups[0].value = ast.copy_location(
            ast.parse("spec.max_iterations == 0 or iteration + 1 < spec.max_iterations", mode="eval").body,
            followups[0].value)
        ast.fix_missing_locations(tree)
        namespace = dict(method.__globals__)
        namespace["_aura_iteration_indexes"] = _iteration_indexes
        exec(compile(tree, "<aura-nanobot-unlimited-iterations>", "exec"), namespace)

        class UnlimitedIterationsRunner(AgentRunner):
            _run_core = namespace["_run_core"]

        return UnlimitedIterationsRunner()
    except Exception:
        # Never reveal imported paths, provider content or dynamic exceptions.
        raise UnlimitedIterationsUnavailable("unsupported SDK iteration structure") from None
