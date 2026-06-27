from pathlib import Path
import tempfile
import unittest

from scripts.repository_quality import scan_root, scan_text


class RepositoryQualityTests(unittest.TestCase):
    def test_incomplete_marker_is_reported(self) -> None:
        marker = "T" + "ODO"

        findings = scan_text("docs/example.md", f"{marker}: finish later\n")

        self.assertEqual(1, len(findings))
        self.assertEqual("incomplete-marker", findings[0].code)
        self.assertEqual(1, findings[0].line)

    def test_private_reference_term_requires_allowed_path(self) -> None:
        private_name = "alt" + "yn"

        findings = scan_text("docs/new-doc.md", f"{private_name}-backend\n")

        self.assertEqual(1, len(findings))
        self.assertEqual("private-reference-term", findings[0].code)

    def test_private_reference_term_is_allowed_in_reference_gates(self) -> None:
        private_name = "alt" + "yn"

        findings = scan_text("docs/reference-gates.md", f"{private_name}-backend\n")

        self.assertEqual([], findings)

    def test_scan_root_reads_text_files(self) -> None:
        marker = "F" + "IXME"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "example.md").write_text(
                f"{marker}: later\n",
                encoding="utf-8",
            )

            findings = scan_root(root)

        self.assertEqual(1, len(findings))
        self.assertEqual("docs/example.md", findings[0].path)


if __name__ == "__main__":
    unittest.main()
