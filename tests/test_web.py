from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_cards import make_card

from qq_mcp_server.cards import CharacterCardService
from qq_mcp_server.config import AppConfig
from qq_mcp_server.mcp_server import create_http_app, create_mcp_servers
from qq_mcp_server.rules import RuleIndex
from qq_mcp_server.store import MessageStore


class FakeOneBot:
    async def get_group_list(self) -> list[dict[str, Any]]:
        return [{"group_id": "2", "group_name": "测试群", "member_count": 4}]

    async def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
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


async def test_character_upload_preview_and_confirm(config: AppConfig, tmp_path: Path) -> None:
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
        assert "确认人物卡" in preview.text
        confirmed = await client.post(
            f"/uploads/character-card/{token}/confirm", data={"runtime_policy": "auto"}
        )
        assert confirmed.status_code == 200
        assert "人物卡已更新" in confirmed.text
    character = store.character(str(group["group_key"]))
    assert character is not None
    assert character["current"]["identity"]["name"] == "调查员"


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


async def test_oauth_metadata_aliases_share_one_canonical_resource(
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
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource/mcp/admin",
        f"/.well-known/oauth-protected-resource/mcp/groups/{group['group_key']}",
    ]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [await client.get(path) for path in paths]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert {response.json()["resource"] for response in responses} == {
        "https://mcp.example.com/mcp"
    }
