"""Nightly backups of everything TARS would be hard to rebuild without, and restoring one.

A backup is a dated folder holding the memory vault, config.toml and the data folder (personality,
spend, the action log, parked drafts). It is written to a temporary folder, checked file by file
against the original, and only then given its date, so a half-written copy never looks finished.

Conversations and logs stay out: they are kept a few days on purpose and "forget" has to mean it.
Keys aren't files at all; they stay in the Windows credential store. The action log follows the
same few-day rule inside every backup.
"""

from __future__ import annotations

import shutil
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config

SKIPPED_DATA = {"logs", "transcripts"}
SAFETY = "before-restore"  # the state just before the last restore, so a restore can be undone
_PARTIAL = ".partial-"


class BackupError(Exception):
    pass


def _dated(path: Path) -> date | None:
    try:
        return datetime.strptime(path.name, "%Y-%m-%d").date()
    except ValueError:
        return None


def _files(root: Path) -> dict[str, int]:
    """Relative path -> size, for every file under root."""
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def _copy_state(config: Config, config_path: Path, target: Path) -> None:
    if config.vault.exists():
        shutil.copytree(config.vault, target / "vault")
    if config_path.exists():
        shutil.copy2(config_path, target / "config.toml")
    data = target / "data"
    data.mkdir(parents=True)
    if config.data_dir.exists():
        for item in config.data_dir.iterdir():
            if item.name in SKIPPED_DATA:
                continue
            if item.is_dir():
                shutil.copytree(item, data / item.name)
            else:
                shutil.copy2(item, data / item.name)


def _expected(config: Config, config_path: Path) -> dict[str, int]:
    want = {f"vault/{k}": v for k, v in _files(config.vault).items()}
    if config_path.exists():
        want["config.toml"] = config_path.stat().st_size
    for rel, size in _files(config.data_dir).items():
        if rel.split("/", 1)[0] not in SKIPPED_DATA:
            want[f"data/{rel}"] = size
    return want


def verify(config: Config, config_path: Path, target: Path) -> list[str]:
    """What's wrong with a fresh copy; empty when it matches the original and reads back."""
    problems = []
    got = _files(target)
    for rel, size in _expected(config, config_path).items():
        if rel not in got:
            problems.append(f"{rel} is missing")
        elif got[rel] != size:
            problems.append(f"{rel} is {got[rel]} bytes, the original is {size}")
    if (target / "config.toml").exists():
        try:
            tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            problems.append(f"config.toml doesn't read back: {e}")
    return problems


def _prune_history(folder: Path, keep_days: int, today: date) -> None:
    """Dated history files (the action log) last as long in a backup as they do live."""
    cutoff = today - timedelta(days=keep_days)
    for path in (folder / "data" / "actions").glob("*.jsonl"):
        try:
            if datetime.strptime(path.stem, "%Y-%m-%d").date() < cutoff:
                path.unlink()
        except ValueError:
            continue


def backup(config: Config, config_path: Path, today: date | None = None, name: str | None = None) -> Path:
    today = today or date.today()
    root = config.backups
    root.mkdir(parents=True, exist_ok=True)
    for stale in root.glob(_PARTIAL + "*"):  # left by a backup that was cut off
        shutil.rmtree(stale, ignore_errors=True)
    final = root / (name or today.isoformat())
    partial = root / f"{_PARTIAL}{final.name}"
    partial.mkdir()
    try:
        _copy_state(config, config_path, partial)
        if problems := verify(config, config_path, partial):
            raise BackupError("The backup copy doesn't match: " + "; ".join(problems[:3]))
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    if final.exists():
        shutil.rmtree(final)
    partial.rename(final)

    cutoff = today - timedelta(days=config.privacy.backup_days)
    for child in root.iterdir():
        stamp = _dated(child)
        if stamp is not None and stamp < cutoff:
            shutil.rmtree(child)
        elif child.is_dir():
            _prune_history(child, config.privacy.transcript_days, today)
    return final


def available(config: Config) -> list[Path]:
    """Backups that can be restored, newest first, with the pre-restore copy at the end."""
    root = config.backups
    if not root.exists():
        return []
    dated = sorted((p for p in root.iterdir() if p.is_dir() and _dated(p)), key=lambda p: p.name, reverse=True)
    return dated + ([root / SAFETY] if (root / SAFETY).is_dir() else [])


def _replace_dir(source: Path, dest: Path) -> None:
    """Swap a folder in whole: copy beside it, then rename, so a failure leaves the original."""
    staging = dest.with_name(dest.name + ".restoring")
    old = dest.with_name(dest.name + ".old")
    for leftover in (staging, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    shutil.copytree(source, staging)
    try:
        if dest.exists():
            dest.rename(old)
        try:
            staging.rename(dest)
        except OSError:
            if old.exists():
                old.rename(dest)
            raise
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise BackupError(f"Windows wouldn't let go of {dest.name} ({e.strerror or e}). "
                          "Close TARS and Obsidian, then try again.") from e
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def restore(config: Config, config_path: Path, source: Path) -> Path:
    """Put a backup back. The current state is saved first as `before-restore`; returns that copy."""
    if not source.is_dir():
        raise BackupError(f"There's no backup at {source}.")
    if any((source / part).exists() for part in ("vault", "config.toml", "data")):
        vault, settings, data = source / "vault", source / "config.toml", source / "data"
    elif (source / "Memory.md").exists():  # older backups held only the vault
        vault, settings, data = source, None, None
    else:
        raise BackupError(f"{source.name} doesn't look like a TARS backup.")

    safety = None
    if source.name != SAFETY:
        safety = backup(config, config_path, name=SAFETY)

    if vault.is_dir():  # a backup from before the vault existed leaves today's alone
        _replace_dir(vault, config.vault)
    if settings is not None and settings.exists():
        tomllib.loads(settings.read_text(encoding="utf-8"))  # never put back a config that won't load
        shutil.copy2(settings, config_path)
    if data is not None and data.is_dir():
        config.data_dir.mkdir(parents=True, exist_ok=True)
        for item in config.data_dir.iterdir():  # what the backup holds is replaced as a whole
            if item.name in SKIPPED_DATA:
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        for item in data.iterdir():
            if item.is_dir():
                shutil.copytree(item, config.data_dir / item.name)
            else:
                shutil.copy2(item, config.data_dir / item.name)
    return safety or source
