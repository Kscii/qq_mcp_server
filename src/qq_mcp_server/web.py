from __future__ import annotations

import html
import secrets
from pathlib import Path

from fastmcp import FastMCP
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from qq_mcp_server.cards import CharacterCardService
from qq_mcp_server.config import AppConfig
from qq_mcp_server.onebot import OneBotClient
from qq_mcp_server.store import MessageStore


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;
line-height:1.55;color:#172033}} table{{width:100%;border-collapse:collapse}}
th,td{{border-bottom:1px solid #d8deea;padding:.65rem;text-align:left}}
button{{padding:.55rem .85rem;margin:.2rem;border:0;border-radius:.4rem;background:#2762d7;color:white}}
.danger{{background:#b42318}} .card{{padding:1rem;border:1px solid #d8deea;border-radius:.6rem}}
.muted{{color:#667085}} code{{word-break:break-all}} input[type=file]{{max-width:100%}}
</style></head><body><h1>{title}</h1>{body}</body></html>""".format(
            title=html.escape(title), body=body
        ),
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _error(error: Exception, *, status: int = 400) -> HTMLResponse:
    return _page(
        "操作未完成",
        f'<div class="card"><p>{html.escape(str(error))}</p>'
        "<p>请回到 ChatGPT 重新获取链接或按提示修正。</p></div>",
        status=status,
    )


def register_web_routes(
    mcp: FastMCP,
    *,
    config: AppConfig,
    store: MessageStore,
    client: OneBotClient,
    cards: CharacterCardService,
) -> None:
    @mcp.custom_route("/admin/groups/{token}", methods=["GET", "POST"], include_in_schema=False)
    async def group_whitelist(request: Request) -> Response:
        token = str(request.path_params["token"])
        try:
            store.capability(token, kind="group_whitelist")
            joined = await client.get_group_list()
            joined_by_id = {str(item["group_id"]): item for item in joined}
            if request.method == "POST":
                form = await request.form()
                action = str(form.get("action") or "")
                if action == "add":
                    group_id = str(form.get("group_id") or "")
                    if group_id not in joined_by_id:
                        raise ValueError("该群不在当前 QQ 已加入群列表中")
                    item = joined_by_id[group_id]
                    group = store.whitelist_group(group_id, str(item["group_name"]))
                    message = (
                        f"已将 {html.escape(str(item['group_name']))} 加入白名单。"
                        f"群 App 地址：<code>{html.escape(_group_mcp_url(config, str(group['group_key'])))}</code>"
                    )
                elif action == "remove":
                    group_key = str(form.get("group_key") or "")
                    group = store.get_group(group_key)
                    store.remove_from_whitelist(group_key)
                    message = (
                        f"已将 {html.escape(str(group['qq_group_name']))} 移出白名单并停止同步。"
                    )
                else:
                    raise ValueError("未知白名单操作")
                store.capability(token, kind="group_whitelist", consume=True)
                return _page(
                    "白名单已更新",
                    f'<div class="card"><p>{message}</p><p>请返回 ChatGPT 继续配置。</p></div>',
                )

            whitelisted = {str(item["qq_group_id"]): item for item in store.list_groups()}
            rows: list[str] = []
            for item in joined:
                group_id = str(item["group_id"])
                name = html.escape(str(item["group_name"]))
                if group_id in whitelisted:
                    group = whitelisted[group_id]
                    action = (
                        '<form method="post"><input type="hidden" name="action" value="remove">'
                        f'<input type="hidden" name="group_key" value="{html.escape(str(group["group_key"]))}">'
                        '<button class="danger" type="submit">移出白名单</button></form>'
                    )
                else:
                    action = (
                        '<form method="post"><input type="hidden" name="action" value="add">'
                        f'<input type="hidden" name="group_id" value="{html.escape(group_id)}">'
                        '<button type="submit">加入白名单</button></form>'
                    )
                rows.append(
                    f"<tr><td>{name}</td><td>{html.escape(group_id)}</td>"
                    f"<td>{int(item['member_count'])}</td><td>{action}</td></tr>"
                )
            body = (
                '<p class="muted">这里只维护数据采集白名单。模组、成员角色和启停继续在管理 App 中完成。</p>'
                "<table><thead><tr><th>群</th><th>群号</th><th>人数</th><th>操作</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table>"
            )
            return _page("QQ群白名单", body)
        except Exception as error:
            return _error(error)

    @mcp.custom_route(
        "/uploads/character-card/{token}", methods=["GET", "POST"], include_in_schema=False
    )
    async def upload_card(request: Request) -> Response:
        token = str(request.path_params["token"])
        try:
            capability = store.capability(token, kind="character_card")
            group_key = str(capability["group_key"])
            group = store.get_group(group_key)
            if request.method == "GET":
                body = f"""
<p>目标群：<strong>{html.escape(str(group["qq_group_name"]))}</strong></p>
<form class="card" method="post" enctype="multipart/form-data">
<p><input type="file" name="card" accept=".xlsx" required></p>
<p class="muted">只读取固定浅蓝色模板的“人物卡”工作表，最大 16 MiB。</p>
<button type="submit">解析并预览</button></form>"""
                return _page("上传人物卡", body)

            form = await request.form(max_files=1, max_fields=20, max_part_size=17 * 1024 * 1024)
            upload = form.get("card")
            if not isinstance(upload, UploadFile):
                raise ValueError("请选择 .xlsx 人物卡")
            filename = Path(upload.filename or "character.xlsx").name
            if not filename.lower().endswith(".xlsx"):
                raise ValueError("人物卡必须是 .xlsx 文件")
            data = await upload.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise ValueError("人物卡文件不能超过 16 MiB")
            incoming = cards.staging_dir / f".incoming-{secrets.token_hex(8)}.xlsx"
            incoming.write_bytes(data)
            incoming.chmod(0o600)
            try:
                staged, parsed = cards.stage(token, incoming)
            finally:
                incoming.unlink(missing_ok=True)
            preview = cards.preview(group_key, parsed)
            store.set_capability_payload(
                token,
                kind="character_card",
                payload={
                    "staged_path": str(staged),
                    "source_filename": filename,
                    "preview": preview,
                },
            )
            default_policy = str(preview["default_runtime_policy"])
            body = f"""
<div class="card"><p><strong>人物：</strong>{html.escape(str(preview["character_name"]))}</p>
<p><strong>玩家：</strong>{html.escape(str(preview.get("player") or "未填写"))}</p>
<p><strong>职业：</strong>{html.escape(str(preview.get("occupation") or "未填写"))}</p>
<p>技能 {preview["skill_count"]} 项，武器 {preview["weapon_count"]} 项，物品 {preview["inventory_count"]} 项。</p>
<p><strong>当前人物：</strong>{html.escape(str(preview.get("previous_character_name") or "尚无"))}</p></div>
<form method="post" action="/uploads/character-card/{html.escape(token)}/confirm">
<p><label><input type="radio" name="runtime_policy" value="auto" checked>
自动（同名保留、异名清空；本次默认 {html.escape(default_policy)}）</label></p>
<p><label><input type="radio" name="runtime_policy" value="preserve">保留动态卡与团务笔记</label></p>
<p><label><input type="radio" name="runtime_policy" value="reset">清空动态卡与团务笔记</label></p>
<button type="submit">确认替换当前人物卡</button></form>"""
            return _page("确认人物卡", body)
        except Exception as error:
            return _error(error)

    @mcp.custom_route(
        "/uploads/character-card/{token}/confirm", methods=["POST"], include_in_schema=False
    )
    async def confirm_card(request: Request) -> Response:
        token = str(request.path_params["token"])
        try:
            capability = store.capability(token, kind="character_card")
            payload = capability["payload"]
            staged_path = Path(str(payload.get("staged_path") or ""))
            if (  # noqa: ASYNC240 - a single local metadata check, no network filesystem
                not staged_path.is_file()  # noqa: ASYNC240 - local metadata only
                or staged_path.parent != cards.staging_dir
            ):
                raise ValueError("待确认人物卡不存在，请重新上传")
            form = await request.form()
            policy = str(form.get("runtime_policy") or "auto")
            result = cards.finalize(
                str(capability["group_key"]),
                staged_path=staged_path,
                source_filename=str(payload.get("source_filename") or "character.xlsx"),
                runtime_policy=policy,
            )
            store.capability(token, kind="character_card", consume=True)
            warning = ""
            if result["warnings"]:
                warning = (
                    "<p>未能保留的字段：" + html.escape("；".join(result["warnings"])) + "</p>"
                )
            return _page(
                "人物卡已更新",
                f'<div class="card"><p>当前人物：<strong>{html.escape(str(result["character_name"]))}</strong></p>'
                f"<p>运行数据策略：{html.escape(str(result['runtime_policy']))}</p>{warning}"
                "<p>请返回 ChatGPT 继续。</p></div>",
            )
        except Exception as error:
            return _error(error)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> Response:
        return Response("ok", media_type="text/plain")


def _base_url(config: AppConfig) -> str:
    if config.public_url:
        return config.public_url
    host = "127.0.0.1" if config.host in {"0.0.0.0", "::"} else config.host
    return f"http://{host}:{config.port}"


def _group_mcp_url(config: AppConfig, group_key: str) -> str:
    return f"{_base_url(config)}/mcp/groups/{group_key}"


def admin_page_url(config: AppConfig, token: str) -> str:
    return f"{_base_url(config)}/admin/groups/{token}"


def card_upload_url(config: AppConfig, token: str) -> str:
    return f"{_base_url(config)}/uploads/character-card/{token}"
