"""Canonical user folders and backward-compatible legacy locations."""
from pathlib import Path

def folder(root: Path, kind: str) -> Path:
    root = Path(root)
    choices = {
        "original": ("original", "originals"),
        "translation": ("traduction/ai", "ai"),
        "jobs": ("traduction/jobs", "ai-jobs"),
        "artifacts": ("traduction/artifacts", "artifacts"),
        "export": ("export", "exports"),
    }
    preferred, legacy = choices[kind]
    return root / preferred if (root / preferred.split("/")[0]).exists() else root / legacy
