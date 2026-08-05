"""Finding the Godot project root from what the client said in ``initialize``."""

from __future__ import annotations

import os
import tempfile
import unittest

from gdscript_lsp_bridge import paths, project


def make_project(directory: str) -> str:
    """Creates a minimal Godot project at ``directory`` and returns it."""
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, project.PROJECT_MARKER), "w", encoding="utf-8") as handle:
        handle.write('config_version=5\n')
    return directory


class RootFromInitializeTest(unittest.TestCase):
    """The workspace is read out of the params, never configured."""

    def test_workspace_folders_win(self) -> None:
        params = {
            "rootUri": "file:///ignored",
            "rootPath": "/also/ignored",
            "workspaceFolders": [{"uri": "file:///wanted", "name": "wanted"}],
        }
        self.assertEqual(project.root_from_initialize(params), "/wanted")

    def test_root_uri_is_used_when_there_are_no_folders(self) -> None:
        params = {"rootUri": "file:///wanted", "rootPath": "/ignored"}
        self.assertEqual(project.root_from_initialize(params), "/wanted")

    def test_root_path_is_the_last_resort(self) -> None:
        self.assertEqual(project.root_from_initialize({"rootPath": "/w"}), "/w")

    def test_percent_encoded_workspace_is_decoded(self) -> None:
        params = {"rootUri": "file:///Users/me/my%20game"}
        self.assertEqual(project.root_from_initialize(params), "/Users/me/my game")

    def test_null_root_uri_falls_through_to_root_path(self) -> None:
        params = {"rootUri": None, "rootPath": "/w"}
        self.assertEqual(project.root_from_initialize(params), "/w")

    def test_nothing_usable_returns_empty(self) -> None:
        self.assertEqual(project.root_from_initialize({}), "")
        self.assertEqual(project.root_from_initialize({"workspaceFolders": []}), "")


class FindProjectRootTest(unittest.TestCase):
    """Search order: the directory, then ancestors, then a bounded descent."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.base = os.path.realpath(self._temporary.name)

    def test_the_workspace_itself(self) -> None:
        make_project(self.base)
        self.assertEqual(project.find_project_root(self.base), self.base)

    def test_an_ancestor_of_the_workspace(self) -> None:
        make_project(self.base)
        deep = os.path.join(self.base, "scripts", "player")
        os.makedirs(deep)
        self.assertEqual(project.find_project_root(deep), self.base)

    def test_a_child_of_the_workspace(self) -> None:
        game = make_project(os.path.join(self.base, "game"))
        self.assertEqual(project.find_project_root(self.base), game)

    def test_a_grandchild_of_the_workspace(self) -> None:
        game = make_project(os.path.join(self.base, "src", "game"))
        self.assertEqual(project.find_project_root(self.base), game)

    def test_the_nearest_ancestor_wins_over_a_deeper_descendant(self) -> None:
        make_project(self.base)
        make_project(os.path.join(self.base, "sub", "other"))
        start = os.path.join(self.base, "sub")
        os.makedirs(start, exist_ok=True)
        self.assertEqual(project.find_project_root(start), self.base)

    def test_descent_is_deterministic_between_runs(self) -> None:
        make_project(os.path.join(self.base, "zebra"))
        make_project(os.path.join(self.base, "alpha"))
        first = project.find_project_root(self.base)
        second = project.find_project_root(self.base)
        self.assertEqual(first, second)
        self.assertEqual(os.path.basename(first), "alpha")

    def test_ignored_directories_are_not_searched(self) -> None:
        make_project(os.path.join(self.base, "node_modules", "vendored"))
        self.assertEqual(project.find_project_root(self.base), "")

    def test_descent_does_not_go_deeper_than_the_limit(self) -> None:
        make_project(os.path.join(self.base, "a", "b", "c", "deep"))
        self.assertEqual(project.find_project_root(self.base), "")

    def test_no_project_anywhere_returns_empty(self) -> None:
        self.assertEqual(project.find_project_root(self.base), "")

    def test_a_nonexistent_path_returns_empty(self) -> None:
        self.assertEqual(
            project.find_project_root(os.path.join(self.base, "nope", "nope")), ""
        )

    def test_a_file_path_resolves_through_its_directory(self) -> None:
        make_project(self.base)
        script = os.path.join(self.base, "main.gd")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("extends Node\n")
        self.assertEqual(project.find_project_root(script), self.base)

    def test_the_result_is_a_physical_path(self) -> None:
        real = make_project(os.path.join(self.base, "real"))
        link = os.path.join(self.base, "link")
        os.symlink(real, link)
        self.assertEqual(project.find_project_root(link), real)


class RewriteInitializeParamsTest(unittest.TestCase):
    """Repointing a monorepo workspace at the Godot project inside it."""

    def test_every_workspace_field_is_repointed(self) -> None:
        params = {
            "rootUri": "file:///repo",
            "rootPath": "/repo",
            "workspaceFolders": [{"uri": "file:///repo", "name": "repo"}],
            "capabilities": {"textDocument": {}},
        }
        updated = project.rewrite_initialize_params(params, "/repo/game")
        self.assertEqual(updated["rootPath"], "/repo/game")
        self.assertEqual(updated["rootUri"], paths.path_to_uri("/repo/game"))
        self.assertEqual(len(updated["workspaceFolders"]), 1)
        self.assertEqual(updated["workspaceFolders"][0]["name"], "game")

    def test_unrelated_params_are_preserved(self) -> None:
        params = {"rootUri": "file:///repo", "capabilities": {"marker": 1}}
        updated = project.rewrite_initialize_params(params, "/repo/game")
        self.assertEqual(updated["capabilities"], {"marker": 1})

    def test_the_original_params_are_not_mutated(self) -> None:
        params = {"rootUri": "file:///repo"}
        project.rewrite_initialize_params(params, "/repo/game")
        self.assertEqual(params["rootUri"], "file:///repo")


if __name__ == "__main__":
    unittest.main()
