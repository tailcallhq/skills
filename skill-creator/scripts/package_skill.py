#!/usr/bin/env python3
# Modified by Tailcall for Forge, 2026 — original: anthropics/skills
"""
Skill Packager - Creates a distributable .skill file of a skill folder

Usage:
    python -m scripts.package_skill <path/to/skill-folder> [-o OUTPUT_DIR]

Example:
    python -m scripts.package_skill skills/public/my-skill
    python -m scripts.package_skill skills/public/my-skill -o ./dist

With no `-o`, the archive is written to a fresh temp directory and its path is
printed. It deliberately does *not* default to the current directory: packaging
is usually run from inside a repository checkout, where dropping a `.skill`
artifact into the working tree means it gets committed by accident.
"""

import argparse
import fnmatch
import sys
import tempfile
import zipfile
from pathlib import Path
from scripts.quick_validate import validate_skill

# Patterns to exclude when packaging skills.
EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
# Directories excluded only at the skill root (not when nested deeper).
ROOT_EXCLUDE_DIRS = {"evals"}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging."""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    # rel_path is relative to skill_path.parent, so parts[0] is the skill
    # folder name and parts[1] (if present) is the first subdir.
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def package_skill(skill_path, output_dir=None):
    """
    Package a skill folder into a .skill file.

    Args:
        skill_path: Path to the skill folder
        output_dir: Output directory for the .skill file. When omitted, a new
            temp directory is created — never the current working directory,
            which is typically a repo checkout the artifact shouldn't pollute.

    Returns:
        Path to the created .skill file, or None if error
    """
    skill_path = Path(skill_path).resolve()

    # Validate skill folder exists
    if not skill_path.exists():
        print(f"❌ Error: Skill folder not found: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"❌ Error: Path is not a directory: {skill_path}")
        return None

    # Validate SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"❌ Error: SKILL.md not found in {skill_path}")
        return None

    # Run validation before packaging
    print("🔍 Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"❌ Validation failed: {message}")
        print("   Please fix the validation errors before packaging.")
        return None
    print(f"✅ {message}\n")

    # Determine output location
    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path(tempfile.mkdtemp(prefix=f"skill-package-{skill_name}-"))

    skill_filename = output_path / f"{skill_name}.skill"

    # Create the .skill file (zip format)
    try:
        with zipfile.ZipFile(skill_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Walk through the skill directory, excluding build artifacts
            for file_path in skill_path.rglob('*'):
                if not file_path.is_file():
                    continue
                arcname = file_path.relative_to(skill_path.parent)
                if should_exclude(arcname):
                    print(f"  Skipped: {arcname}")
                    continue
                zipf.write(file_path, arcname)
                print(f"  Added: {arcname}")

        print(f"\n✅ Successfully packaged skill to: {skill_filename}")
        if not output_dir:
            print("   (temp directory — pass -o/--output to choose a location)")
        return skill_filename

    except Exception as e:
        print(f"❌ Error creating .skill file: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Package a skill folder into a distributable .skill file"
    )
    parser.add_argument("skill_path", help="Path to the skill folder")
    parser.add_argument(
        "-o", "--output",
        dest="output_dir",
        default=None,
        help="Directory to write the .skill file to (default: a new temp directory)",
    )
    # Positional output dir kept for compatibility with the original CLI.
    parser.add_argument("output_positional", nargs="?", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    output_dir = args.output_dir or args.output_positional

    print(f"📦 Packaging skill: {args.skill_path}")
    if output_dir:
        print(f"   Output directory: {output_dir}")
    else:
        print("   Output directory: (temp dir; use -o to choose)")
    print()

    result = package_skill(args.skill_path, output_dir)

    if result:
        # Printed last and bare so a caller can capture it with `tail -1`.
        print(result)
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
