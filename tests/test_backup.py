from datetime import date

import pytest

from tars.actionlog import ActionLog
from tars.backup import SAFETY, BackupError, available, backup, restore, verify
from tars.config import Config
from tars.memory import Vault
from tars.transcripts import Transcripts


@pytest.fixture
def setup(tmp_path):
    config = Config(home=tmp_path / "home")
    path = config.home / "config.toml"
    config.home.mkdir()
    path.write_text('user_name = "Evan"  # how TARS refers to you\n', encoding="utf-8")
    vault = Vault(config.vault)
    vault.ensure()
    vault.remember("Sam's birthday is June 3", "People")
    config.data_dir.mkdir()
    (config.data_dir / "personality.json").write_text('{"humor": 40}', encoding="utf-8")
    ActionLog(config.data_dir).record("add_task", "Todoist", "Added 'buy milk'", "spoken")
    Transcripts(config.data_dir).append("user", "what's my day like")
    (config.data_dir / "logs").mkdir()
    (config.data_dir / "logs" / "tars.log").write_text("said things", encoding="utf-8")
    return config, path, vault


def test_backup_then_restore_puts_everything_back(setup):
    config, path, vault = setup
    target = backup(config, path, today=date(2026, 10, 9))
    assert target.name == "2026-10-09"
    assert (target / "vault" / "Memory.md").exists() and (target / "config.toml").exists()
    assert (target / "data" / "personality.json").exists() and list((target / "data" / "actions").iterdir())
    # Conversations and logs stay out: they're kept a few days on purpose.
    assert not (target / "data" / "transcripts").exists() and not (target / "data" / "logs").exists()
    assert verify(config, path, target) == []

    # Lose things after the backup.
    vault.forget("birthday")
    path.write_text('user_name = "Someone else"\n', encoding="utf-8")
    (config.data_dir / "personality.json").unlink()
    (config.data_dir / "stray.json").write_text("{}", encoding="utf-8")

    restore(config, path, target)
    assert "Sam's birthday" in Vault(config.vault).snapshot()
    assert path.read_text(encoding="utf-8").startswith('user_name = "Evan"  # how TARS refers to you')
    assert (config.data_dir / "personality.json").read_text(encoding="utf-8") == '{"humor": 40}'
    assert not (config.data_dir / "stray.json").exists()
    assert ActionLog(config.data_dir).recent()[0]["summary"] == "Added 'buy milk'"
    # Live conversations and logs are left alone by a restore.
    assert Transcripts(config.data_dir).recent() and (config.data_dir / "logs" / "tars.log").exists()

    # The state before the restore was kept, so the restore can be undone.
    assert [p.name for p in available(config)] == ["2026-10-09", SAFETY]
    restore(config, path, config.backups / SAFETY)
    assert "Sam's birthday" not in Vault(config.vault).snapshot()
    assert (config.data_dir / "stray.json").exists()


def test_old_copies_and_old_history_are_pruned(setup):
    config, path, _ = setup
    backup(config, path, today=date(2026, 9, 1))
    old_actions = config.backups / "2026-09-01" / "data" / "actions"
    (old_actions / "2026-08-01.jsonl").write_text("{}\n", encoding="utf-8")
    backup(config, path, today=date(2026, 9, 5))
    assert not (old_actions / "2026-08-01.jsonl").exists()  # history lasts as long in a backup as live
    backup(config, path, today=date(2026, 10, 8))
    assert [p.name for p in available(config)] == ["2026-10-08"]  # 30 days kept


def test_a_cut_off_backup_never_looks_finished(setup, monkeypatch):
    config, path, _ = setup
    (config.backups / ".partial-2026-10-01").mkdir(parents=True)
    monkeypatch.setattr("tars.backup.verify", lambda *a: ["vault/Memory.md is missing"])
    with pytest.raises(BackupError, match="doesn't match"):
        backup(config, path, today=date(2026, 10, 9))
    assert list(config.backups.iterdir()) == []


def test_restore_reads_older_vault_only_backups_and_refuses_junk(setup):
    config, path, vault = setup
    old = config.backups / "2026-10-01"
    old.mkdir(parents=True)
    (old / "Memory.md").write_text("# Memory\n\n## People\n- Old fact (2026-10-01)\n", encoding="utf-8")
    restore(config, path, old)
    assert "Old fact" in Vault(config.vault).snapshot()
    assert path.read_text(encoding="utf-8").startswith('user_name = "Evan"')  # untouched

    junk = config.backups / "2026-10-02"
    junk.mkdir()
    with pytest.raises(BackupError, match="doesn't look like"):
        restore(config, path, junk)


def test_a_locked_vault_leaves_everything_as_it_was(setup, monkeypatch):
    from pathlib import Path

    config, path, vault = setup
    target = backup(config, path, today=date(2026, 10, 9))
    vault.remember("Added after the backup", "Other")
    real_rename = Path.rename

    def locked(self, dest):
        if self == config.vault:
            raise PermissionError(13, "The process cannot access the file")
        return real_rename(self, dest)

    monkeypatch.setattr(Path, "rename", locked)
    with pytest.raises(BackupError, match="Close TARS and Obsidian"):
        restore(config, path, target)
    assert "Added after the backup" in Vault(config.vault).snapshot()
    assert not config.vault.with_name("vault.restoring").exists()


def test_a_backup_from_before_the_vault_existed_still_restores(tmp_path):
    config = Config(home=tmp_path / "home")
    config.home.mkdir()
    path = config.home / "config.toml"
    path.write_text('user_name = "Evan"\n', encoding="utf-8")
    target = backup(config, path, today=date(2026, 10, 9))
    path.write_text('user_name = "Changed"\n', encoding="utf-8")
    restore(config, path, target)
    assert path.read_text(encoding="utf-8") == 'user_name = "Evan"\n'
