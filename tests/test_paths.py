"""Project-key derivation: two spellings collapse, two projects never do.

These are the properties the whole warm-reuse scheme rests on. If spellings did
not collapse, every differently-spelled invocation would spawn its own engine;
if distinct roots collided, one project would be served another's symbols.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from gdscript_lsp_bridge import paths


class ProjectKeyTest(unittest.TestCase):
    """Two spellings of one root produce one key; different roots do not."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_trailing_separator_is_irrelevant(self) -> None:
        project = os.path.join(self.base, "game")
        os.makedirs(project)
        self.assertEqual(
            paths.project_key(project), paths.project_key(project + os.sep)
        )

    def test_relative_and_absolute_spellings_agree(self) -> None:
        project = os.path.join(self.base, "game")
        os.makedirs(project)
        previous = os.getcwd()
        os.chdir(self.base)
        try:
            self.assertEqual(paths.project_key("game"), paths.project_key(project))
        finally:
            os.chdir(previous)

    def test_dot_segments_are_normalized_away(self) -> None:
        project = os.path.join(self.base, "game")
        os.makedirs(project)
        noisy = os.path.join(self.base, "game", ".", "..", "game")
        self.assertEqual(paths.project_key(noisy), paths.project_key(project))

    def test_symlinked_ancestor_resolves_to_the_physical_root(self) -> None:
        real = os.path.join(self.base, "real")
        os.makedirs(os.path.join(real, "game"))
        link = os.path.join(self.base, "link")
        os.symlink(real, link)
        self.assertEqual(
            paths.project_key(os.path.join(link, "game")),
            paths.project_key(os.path.join(real, "game")),
        )

    def test_case_differences_collapse(self) -> None:
        project = os.path.join(self.base, "Game")
        os.makedirs(project)
        # Deliberate: the scheme folds case unconditionally, so these agree
        # whatever the volume does. paths.py documents the guard that keeps
        # that safe on a case-sensitive filesystem.
        self.assertEqual(
            paths.project_key(os.path.join(self.base, "Game")),
            paths.project_key(os.path.join(self.base, "game")),
        )

    def test_different_roots_produce_different_keys(self) -> None:
        first = os.path.join(self.base, "alpha")
        second = os.path.join(self.base, "beta")
        os.makedirs(first)
        os.makedirs(second)
        self.assertNotEqual(paths.project_key(first), paths.project_key(second))

    def test_sibling_prefix_roots_do_not_collide(self) -> None:
        first = os.path.join(self.base, "game")
        second = os.path.join(self.base, "game2")
        os.makedirs(first)
        os.makedirs(second)
        self.assertNotEqual(paths.project_key(first), paths.project_key(second))

    def test_key_shape_is_32_lowercase_hex_characters(self) -> None:
        key = paths.project_key(self.base)
        self.assertEqual(len(key), 32)
        self.assertTrue(all(character in "0123456789abcdef" for character in key))

    def test_filesystem_root_is_not_flattened_to_empty(self) -> None:
        # Stripping separators from "/" would leave "", keying every root the
        # same. physical_root guards that case.
        self.assertTrue(paths.physical_root(os.sep))


class UriConversionTest(unittest.TestCase):
    """URI conversion survives the round trip and the awkward characters."""

    def test_round_trip_preserves_a_plain_path(self) -> None:
        path = "/Users/someone/code/game"
        self.assertEqual(paths.uri_to_path(paths.path_to_uri(path)), path)

    def test_round_trip_preserves_spaces_and_percent(self) -> None:
        path = "/Users/someone/my project/100% done"
        self.assertEqual(paths.uri_to_path(paths.path_to_uri(path)), path)

    def test_percent_encoding_is_decoded(self) -> None:
        self.assertEqual(
            paths.uri_to_path("file:///Users/someone/my%20project"),
            "/Users/someone/my project",
        )

    def test_a_bare_path_passes_through_unchanged(self) -> None:
        self.assertEqual(paths.uri_to_path("/plain/path"), "/plain/path")

    def test_empty_input_is_empty_output(self) -> None:
        self.assertEqual(paths.uri_to_path(""), "")

    def test_windows_style_uri_drops_the_leading_separator(self) -> None:
        self.assertEqual(paths.uri_to_path("file:///C:/games/thing"), "C:/games/thing")


if __name__ == "__main__":
    unittest.main()
