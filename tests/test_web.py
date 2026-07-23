from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp.server.auth import AccessToken
from test_cards import make_card

from qq_mcp_server.cards import CharacterCardService
from qq_mcp_server.config import AppConfig
from qq_mcp_server.mcp_server import create_http_app, create_mcp_servers
from qq_mcp_server.rules import RuleIndex
from qq_mcp_server.store import MessageStore


class FakeOneBot:
    async def get_login_info(self) -> dict[str, Any]:
        return {"user_id": "1"}

    async def get_group_list(self) -> list[dict[str, Any]]:
        return [{"group_id": "2", "group_name": "测试群", "member_count": 4}]

    async def get_group_info(self, group_id: str, *, no_cache: bool = False) -> dict[str, Any]:
        return {"group_id": group_id, "group_name": f"候选群-{group_id}"}

    async def get_group_member_list(
        self, group_id: str, *, no_cache: bool = False
    ) -> list[dict[str, Any]]:
        return [{"qq_user_id": "1", "display_name": "测试账号"}] if no_cache else []

    async def get_group_history(
        self, group_id: str, count: int, *, message_seq: str | None = None
    ) -> list[dict[str, Any]]:
        return []


def services(config: AppConfig) -> tuple[MessageStore, FakeOneBot, CharacterCardService, Any]:
    store = MessageStore(config.database_path)
    onebot = FakeOneBot()
    cards = CharacterCardService(store, config.card_storage_dir)
    admin, group = create_mcp_servers(
        config,
        store,
        onebot,  # type: ignore[arg-type]
        RuleIndex(config.rules_database_path),
        cards,
    )
    return store, onebot, cards, create_http_app(admin, group, store)


async def test_whitelist_web_page_and_group_route_guard(config: AppConfig) -> None:
    store, _, _, app = services(config)
    token = store.issue_capability(
        kind="group_whitelist", group_key=None, issued_to="local", ttl_seconds=600
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/healthz")).text == "ok"
        assert (await client.post("/mcp/groups/not-whitelisted")).status_code == 404
        page = await client.get(f"/admin/groups/{token}")
        assert page.status_code == 200
        assert "测试群" in page.text
        result = await client.post(
            f"/admin/groups/{token}", data={"action": "add", "group_id": "2"}
        )
        assert result.status_code == 200
        assert "群 App 地址" in result.text
    assert store.get_group_by_qq("2") is not None


async def test_character_upload_preview_and_confirm(
    config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, cards, app = services(config)
    group = store.whitelist_group("2", "测试群")
    token = store.issue_capability(
        kind="character_card",
        group_key=str(group["group_key"]),
        issued_to="local",
        ttl_seconds=600,
    )
    card_path = make_card(tmp_path / "调查员.xlsx")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        preview = await client.post(
            f"/uploads/character-card/{token}",
            files={
                "card": (
                    card_path.name,
                    card_path.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert preview.status_code == 200
        assert "确认人物卡" in preview.text

        def unexpected_reparse(_: Path) -> None:
            raise AssertionError("确认阶段不应再次解析人物卡")

        monkeypatch.setattr(cards.parser, "parse", unexpected_reparse)
        confirmed = await client.post(
            f"/uploads/character-card/{token}/confirm", data={"runtime_policy": "auto"}
        )
        assert confirmed.status_code == 200
        assert "人物卡已更新" in confirmed.text
    character = store.character(str(group["group_key"]))
    assert character is not None
    assert character["current"]["identity"]["name"] == "调查员"


async def test_event_candidate_can_be_directly_verified_before_whitelist(
    config: AppConfig,
) -> None:
    store, _, _, app = services(config)
    store.upsert_group_candidate(
        "3",
        "事件发现群",
        source="group_message_event",
    )
    token = store.issue_capability(
        kind="group_whitelist", group_key=None, issued_to="local", ttl_seconds=600
    )
    store.set_capability_payload(
        token,
        kind="group_whitelist",
        payload={"group_id": "3"},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        preview = await client.get(f"/admin/groups/{token}")
        confirmed = await client.post(
            f"/admin/groups/{token}",
            data={"action": "add", "group_id": "3"},
        )

    assert "事件发现群" in preview.text
    assert confirmed.status_code == 200
    assert store.get_group_by_qq("3") is not None


async def test_napcat_launcher_hides_token_until_confirmed_redirect(
    config: AppConfig, tmp_path: Path
) -> None:
    webui_config = tmp_path / "napcat" / "webui.json"
    webui_config.parent.mkdir(parents=True)
    webui_config.write_text('{"token":"private-webui-token"}')
    private_config = replace(
        config,
        napcat_webui_url="https://qq.example-tailnet.ts.net:8443/webui",
        napcat_webui_config_path=webui_config,
    )
    store, _, _, app = services(private_config)
    token = store.issue_capability(
        kind="napcat_webui", group_key=None, issued_to="local", ttl_seconds=600
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        preview = await client.get(f"/admin/napcat/{token}")
        redirected = await client.post(f"/admin/napcat/{token}")
        reused = await client.post(f"/admin/napcat/{token}")

    assert preview.status_code == 200
    assert "private-webui-token" not in preview.text
    assert redirected.status_code == 303
    assert (
        redirected.headers["location"]
        == "https://qq.example-tailnet.ts.net:8443/webui/web_login?token=private-webui-token"
    )
    assert reused.status_code == 400
    assert redirected.headers["cache-control"] == "no-store"


async def test_recovery_requires_post_and_only_writes_fixed_request(
    config: AppConfig,
) -> None:
    store, _, _, app = services(config)
    token = store.issue_capability(
        kind="napcat_recovery", group_key=None, issued_to="local", ttl_seconds=600
    )
    request_path = config.napcat_control_dir / "restart-napcat.request"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        preview = await client.get(f"/admin/napcat-recovery/{token}")
        assert not request_path.exists()
        confirmed = await client.post(f"/admin/napcat-recovery/{token}")

    assert preview.status_code == 200
    assert confirmed.status_code == 200
    assert request_path.is_file()
    assert request_path.stat().st_mode & 0o777 == 0o600


async def test_character_confirm_rejects_changed_staged_file(
    config: AppConfig, tmp_path: Path
) -> None:
    store, _, _, app = services(config)
    group = store.whitelist_group("2", "测试群")
    token = store.issue_capability(
        kind="character_card",
        group_key=str(group["group_key"]),
        issued_to="local",
        ttl_seconds=600,
    )
    card_path = make_card(tmp_path / "调查员.xlsx")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        preview = await client.post(
            f"/uploads/character-card/{token}",
            files={
                "card": (
                    card_path.name,
                    card_path.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert preview.status_code == 200
        payload = store.capability(token, kind="character_card")["payload"]
        staged_path = Path(str(payload["staged_path"]))
        staged_path.write_bytes(  # noqa: ASYNC240 - isolated local test fixture
            staged_path.read_bytes() + b"changed-after-preview"  # noqa: ASYNC240
        )

        confirmed = await client.post(
            f"/uploads/character-card/{token}/confirm", data={"runtime_policy": "auto"}
        )

    assert confirmed.status_code == 400
    assert "预览后发生变化" in confirmed.text
    assert store.character(str(group["group_key"])) is None


async def test_admin_and_dynamic_group_http_endpoints_initialize(config: AppConfig) -> None:
    store, _, _, app = services(config)
    group = store.whitelist_group("2", "测试群")
    other_group = store.whitelist_group("3", "另一个群")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    status_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "trpg.get_status", "arguments": {}},
    }
    inner = app.app  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=app)
    async with (
        inner.router.lifespan_context(inner),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        admin = await client.post("/mcp/admin", json=payload, headers=headers)
        group_app = await client.post(
            f"/mcp/groups/{group['group_key']}", json=payload, headers=headers
        )
        first_status = await client.post(
            f"/mcp/groups/{group['group_key']}", json=status_payload, headers=headers
        )
        second_status = await client.post(
            f"/mcp/groups/{other_group['group_key']}", json=status_payload, headers=headers
        )
    assert admin.status_code == 200
    assert '"name":"TRPG 管理"' in admin.text
    assert group_app.status_code == 200
    assert '"name":"TRPG 群"' in group_app.text
    assert '\\"qq_group_id\\":\\"2\\"' in first_status.text
    assert '\\"qq_group_id\\":\\"3\\"' not in first_status.text
    assert '\\"qq_group_id\\":\\"3\\"' in second_status.text
    assert '\\"qq_group_id\\":\\"2\\"' not in second_status.text


async def test_oauth_metadata_uses_each_exact_mcp_resource(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("MCP_JWT_SIGNING_KEY", "jwt-signing-key-for-tests")
    monkeypatch.setenv("MCP_STORAGE_ENCRYPTION_KEY", "storage-encryption-key-for-tests")
    public_config = replace(
        config,
        public_url="https://mcp.example.com",
        allowed_google_emails=("keeper@example.com",),
    )
    store, _, _, app = services(public_config)
    group = store.whitelist_group("2", "测试群")
    paths = [
        "/.well-known/oauth-protected-resource/mcp/admin",
        f"/.well-known/oauth-protected-resource/mcp/groups/{group['group_key']}",
    ]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [await client.get(path) for path in paths]

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["resource"] for response in responses] == [
        "https://mcp.example.com/mcp/admin",
        f"https://mcp.example.com/mcp/groups/{group['group_key']}",
    ]


async def test_combined_http_app_preserves_bearer_authentication_middleware(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("MCP_JWT_SIGNING_KEY", "jwt-signing-key-for-tests")
    monkeypatch.setenv("MCP_STORAGE_ENCRYPTION_KEY", "storage-encryption-key-for-tests")
    public_config = replace(
        config,
        public_url="https://mcp.example.com",
        allowed_google_emails=("keeper@example.com",),
    )
    store = MessageStore(public_config.database_path)
    onebot = FakeOneBot()
    cards = CharacterCardService(store, public_config.card_storage_dir)
    admin, group_mcp = create_mcp_servers(
        public_config,
        store,
        onebot,  # type: ignore[arg-type]
        RuleIndex(public_config.rules_database_path),
        cards,
    )
    assert admin.auth is not None

    async def verify_token(token: str) -> AccessToken | None:
        assert token == "valid-test-token"
        return AccessToken(
            token=token,
            client_id="keeper",
            scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
            claims={"email": "keeper@example.com"},
        )

    monkeypatch.setattr(admin.auth, "verify_token", verify_token)
    app = create_http_app(admin, group_mcp, store)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    headers = {
        "authorization": "Bearer valid-test-token",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    inner = app.app  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=app)
    async with (
        inner.router.lifespan_context(inner),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        response = await client.post("/mcp/admin", json=payload, headers=headers)

    assert response.status_code == 200
    assert '"name":"TRPG 管理"' in response.text
