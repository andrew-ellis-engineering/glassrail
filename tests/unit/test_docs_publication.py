"""Guards for the boundary between repository and published documentation."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DOCS = (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")))

_EXCLUDE_KEY = re.compile(r"^\s*exclude_docs\s*:", re.IGNORECASE)
_EXCLUDED_SECTION = re.compile(
    r"(?<![\w-])(?:docs[\\/])?(?P<section>specs|release)[\\/]",
    re.IGNORECASE,
)
_PRIVATE_VAULT_PATH = re.compile(r"(?<![\w-])vault[\\/]", re.IGNORECASE)
_INTERNAL_REFERENCE_PATTERNS = (
    ("private-vault path", _PRIVATE_VAULT_PATH),
    ("excluded specs/release path", _EXCLUDED_SECTION),
    (
        "eval-framework/CLAUDE.md",
        re.compile(r"(?<![\w-])eval-framework[\\/]CLAUDE\.md(?![\w.-])", re.IGNORECASE),
    ),
    (
        "EVAL_PLAN.md",
        re.compile(r"(?<![\w-])EVAL_PLAN\.md(?![\w.-])", re.IGNORECASE),
    ),
)


def _exclude_docs_value(config: str) -> str:
    """Return the YAML value for ``exclude_docs`` without requiring PyYAML."""
    lines = config.splitlines()
    for index, line in enumerate(lines):
        if not _EXCLUDE_KEY.match(line):
            continue

        key_indent = len(line) - len(line.lstrip())
        inline_value = line.split(":", 1)[1].strip()
        if inline_value and inline_value[0] not in "|>":
            return inline_value

        block_lines: list[str] = []
        for block_line in lines[index + 1 :]:
            if block_line.strip() and len(block_line) - len(block_line.lstrip()) <= key_indent:
                break
            block_lines.append(block_line)
        return "\n".join(block_lines)

    raise AssertionError("mkdocs.yml does not define exclude_docs")


def _excluded_directories(config: str) -> set[str]:
    value = re.sub(r"#[^\n]*", "", _exclude_docs_value(config))
    return {match.group("section").lower() for match in _EXCLUDED_SECTION.finditer(value)}


def _is_excluded_site_url(url: str) -> bool:
    path_parts = [part.lower() for part in urlsplit(url.rstrip(".,;:!? ")).path.split("/")]
    if "glassrail" not in path_parts:
        return False

    glassrail_index = path_parts.index("glassrail")
    return len(path_parts) > glassrail_index + 1 and path_parts[glassrail_index + 1] in {
        "specs",
        "release",
    }


def _public_doc_violations(path: Path, text: str) -> list[str]:
    violations: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in _INTERNAL_REFERENCE_PATTERNS:
            if pattern.search(line):
                violations.append(
                    f"{path.relative_to(ROOT)}:{line_number}: {label}: {line.strip()}"
                )
        for url in re.findall(r"https?://[^\s<>\]}`\"']+", line, flags=re.IGNORECASE):
            if _is_excluded_site_url(url):
                violations.append(
                    f"{path.relative_to(ROOT)}:{line_number}: excluded site URL: {line.strip()}"
                )
    return violations


def test_mkdocs_excludes_private_documentation_sections() -> None:
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    assert _excluded_directories(config) >= {"specs", "release"}


def test_public_docs_do_not_reference_private_documentation() -> None:
    violations = [
        violation
        for path in PUBLIC_DOCS
        for violation in _public_doc_violations(path, path.read_text(encoding="utf-8"))
    ]

    assert not violations, "private documentation references found:\n" + "\n".join(violations)
