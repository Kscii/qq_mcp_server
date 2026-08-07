from __future__ import annotations

from pathlib import Path

from qq_mcp_server.release import without_release_version


def deploy_text(name: str) -> str:
    return (Path("deploy") / name).read_text(encoding="utf-8")


def test_routine_deploy_only_touches_napcat_during_first_collector_migration() -> None:
    script = deploy_text("deploy.sh")

    assert "compose stop app" in script
    assert "compose up -d --no-deps app" in script
    assert "compose up -d napcat" not in script
    assert "compose pull napcat" not in script
    assert "docker restart" not in script
    assert 'if [ "$collector_first_install" -eq 1 ]' in script
    assert "prepare-napcat /data/napcat/config" in script
    assert "docker update --restart=no qq-mcp-server-napcat" in script
    assert 'elif [ "$deploy_collector" = "1" ]' in script


def test_first_v5_deploy_has_verified_backup_and_rollback() -> None:
    deploy = deploy_text("deploy.sh")
    rollback = deploy_text("deploy-image.sh")

    assert "pre-v5-backup.path" in deploy
    assert "backup \\" in deploy
    assert "--output-dir /data/backups" in deploy
    assert 'if [ "$migrated_schema" != "5" ]' in deploy
    assert "PRAGMA integrity_check" in rollback
    assert "os.replace(temporary_path, destination_path)" in rollback
    assert "SKIP_SAFETY_MIGRATION=1 ./deploy.sh" in rollback


def test_v4_to_v5_migration_does_not_pause_or_restart_napcat() -> None:
    deploy = deploy_text("deploy.sh")

    assert '[ "$schema_version" = "4" ]' in deploy
    assert "v4→v5 只增加归档和补偿状态" in deploy
    assert "status --json" in deploy


def test_account_switch_helper_uses_fixed_request_and_per_account_directory() -> None:
    helper = deploy_text("switch-napcat-account.sh")
    compose = deploy_text("compose.yaml")

    assert "switch-napcat-account.request" in helper
    assert 'account_dir="/var/lib/qq_mcp_server/napcat/accounts/$target/qq"' in helper
    assert "QQ_ACCOUNT_ID" in helper
    assert "NAPCAT_ACCOUNT_DIR" in helper
    assert "${NAPCAT_ACCOUNT_DIR:-" in compose


def test_napcat_lifecycle_requires_explicit_maintenance() -> None:
    compose = deploy_text("compose.yaml")
    maintenance = deploy_text("maintain-napcat.sh")
    recovery = deploy_text("restart-napcat.sh")

    assert 'restart: "no"' in compose
    assert "CONFIRM_NAPCAT_MAINTENANCE" in maintenance
    assert "docker update --restart=no" in maintenance
    assert "max_restarts_per_window=2" in recovery
    assert "restart_window_seconds=86400" in recovery


def test_collector_release_check_ignores_only_project_version() -> None:
    old_project = b'[project]\nname = "qq_mcp_server"\nversion = "0.8.0"\n'
    new_project = b'[project]\nname = "qq_mcp_server"\nversion = "0.8.1"\n'
    old_lock = (
        b'[[package]]\nname = "qq-mcp-server"\nversion = "0.8.0"\nsource = { editable = "." }\n'
    )
    new_lock = (
        b'[[package]]\nname = "qq-mcp-server"\nversion = "0.8.1"\nsource = { editable = "." }\n'
    )

    assert without_release_version(old_project, "pyproject.toml") == without_release_version(
        new_project, "pyproject.toml"
    )
    assert without_release_version(old_lock, "uv.lock") == without_release_version(
        new_lock, "uv.lock"
    )


def test_collector_release_check_keeps_dependency_changes() -> None:
    old_project = b'[project]\nname = "qq_mcp_server"\nversion = "0.8.0"\ndependencies = ["a"]\n'
    new_project = b'[project]\nname = "qq_mcp_server"\nversion = "0.8.1"\ndependencies = ["b"]\n'

    assert without_release_version(old_project, "pyproject.toml") != without_release_version(
        new_project, "pyproject.toml"
    )


def test_release_workflow_uses_collector_image_decision_helper() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'python src/qq_mcp_server/release.py "$previous" HEAD' in workflow
