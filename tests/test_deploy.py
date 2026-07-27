from __future__ import annotations

from pathlib import Path


def deploy_text(name: str) -> str:
    return (Path("deploy") / name).read_text(encoding="utf-8")


def test_routine_deploy_never_starts_or_updates_napcat() -> None:
    script = deploy_text("deploy.sh")

    assert "compose stop app" in script
    assert "compose up -d --no-deps app" in script
    assert "compose up -d napcat" not in script
    assert "compose pull napcat" not in script
    assert "prepare-napcat" not in script
    assert "docker restart" not in script
    assert "docker update" not in script


def test_first_v3_deploy_has_verified_backup_and_v2_rollback() -> None:
    deploy = deploy_text("deploy.sh")
    rollback = deploy_text("deploy-image.sh")

    assert "pre-v3-backup.path" in deploy
    assert "backup \\" in deploy
    assert "--output-dir /data/backups" in deploy
    assert 'if [ "$migrated_schema" != "3" ]' in deploy
    assert "PRAGMA integrity_check" in rollback
    assert "os.replace(temporary_path, destination_path)" in rollback
    assert "SKIP_SAFETY_MIGRATION=1 ./deploy.sh" in rollback


def test_napcat_lifecycle_requires_explicit_maintenance() -> None:
    compose = deploy_text("compose.yaml")
    maintenance = deploy_text("maintain-napcat.sh")
    recovery = deploy_text("restart-napcat.sh")

    assert 'restart: "on-failure:2"' in compose
    assert "CONFIRM_NAPCAT_MAINTENANCE" in maintenance
    assert "max_restarts_per_window=2" in recovery
    assert "restart_window_seconds=86400" in recovery
