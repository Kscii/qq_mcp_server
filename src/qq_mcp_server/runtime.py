from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from qq_mcp_server.config import AppConfig
from qq_mcp_server.normalization import normalize_message
from qq_mcp_server.onebot import (
    OneBotClient,
    OneBotConfigurationError,
    OneBotSessionError,
)
from qq_mcp_server.store import MessageStore
from qq_mcp_server.sync import (
    AccountMismatchError,
    CollectionPausedError,
    MultiGroupSyncManager,
)

LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _age_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return max(0.0, (_utc_now() - datetime.fromisoformat(value)).total_seconds())
    except ValueError:
        return None


def sync_freshness(state: dict[str, Any], maximum_age_seconds: float) -> dict[str, Any]:
    age = _age_seconds(state.get("last_sync_at"))
    reconcile_in_progress = bool(state.get("reconcile_cursor"))
    fresh = (
        age is not None
        and age <= maximum_age_seconds
        and not state.get("last_error")
        and not reconcile_in_progress
    )
    return {
        "fresh": fresh,
        "age_seconds": round(age, 3) if age is not None else None,
        "maximum_age_seconds": maximum_age_seconds,
        "last_sync_at": state.get("last_sync_at"),
        "last_error": state.get("last_error"),
        "reconcile_in_progress": reconcile_in_progress,
    }


class NapCatRuntime:
    """群发现、SSE 实时导入和面向管理 MCP 的诊断状态。"""

    def __init__(
        self,
        config: AppConfig,
        client: OneBotClient,
        store: MessageStore,
        onebot_token: str,
        manager: MultiGroupSyncManager | None = None,
        *,
        sse_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self._onebot_token = onebot_token
        self.manager = manager or MultiGroupSyncManager(config, client, store)
        self._sse_transport = sse_transport
        self._registry_lock = asyncio.Lock()

    async def refresh_registry(self, *, force: bool = False) -> list[dict[str, Any]]:
        self.manager.require_active()
        async with self._registry_lock:
            previous = self.store.runtime_status("group_registry")
            if force:
                last_attempt = previous.get("last_attempt_at")
                if isinstance(last_attempt, str):
                    try:
                        age = (_utc_now() - datetime.fromisoformat(last_attempt)).total_seconds()
                    except ValueError:
                        age = 60
                    if age < 60:
                        raise RuntimeError(
                            f"群列表强制刷新冷却中，请等待 {max(1, int(60 - age))} 秒"
                        )
            attempt_at = _iso_now()
            try:
                async with self.manager.limiter:
                    login = await self.client.get_login_info()
                    actual = str(login.get("user_id") or "")
                    if actual != self.config.account_id:
                        raise AccountMismatchError(
                            f"NapCat 当前登录 QQ {actual or '未知'}，"
                            f"配置要求 {self.config.account_id}"
                        )
                    groups = await self.client.get_group_list()
                for group in groups:
                    self.store.upsert_group_candidate(
                        str(group["group_id"]),
                        str(group["group_name"]),
                        source="forced_group_list",
                    )
                self.store.set_runtime_status(
                    "group_registry",
                    {
                        "ok": True,
                        "account_id": actual,
                        "group_count": len(groups),
                        "group_ids": [str(item["group_id"]) for item in groups],
                        "last_attempt_at": attempt_at,
                        "last_success_at": _iso_now(),
                        "last_error": None,
                    },
                )
                return groups
            except (OneBotSessionError, AccountMismatchError) as error:
                self.manager.pause_session(error, source="group_registry")
                raise
            except OneBotConfigurationError as error:
                self.manager.pause_configuration(error, source="group_registry")
                raise
            except Exception as error:
                self.store.set_runtime_status(
                    "group_registry",
                    {
                        "ok": False,
                        "account_id": previous.get("account_id"),
                        "group_count": previous.get("group_count"),
                        "group_ids": previous.get("group_ids", []),
                        "last_attempt_at": attempt_at,
                        "last_success_at": previous.get("last_success_at"),
                        "last_error": f"{type(error).__name__}: {error}"[:500],
                    },
                )
                raise

    async def probe_group(self, group_id: str) -> dict[str, Any]:
        if not group_id.isdigit():
            raise ValueError("group_id 只能包含数字")
        self.manager.require_active()
        registry_error: Exception | None = None
        groups: list[dict[str, Any]] = []
        try:
            groups = await self.refresh_registry(force=True)
        except CollectionPausedError:
            raise
        except (OneBotSessionError, AccountMismatchError, OneBotConfigurationError):
            raise
        except Exception as error:
            registry_error = error
        self.manager.require_active()
        listed = next(
            (item for item in groups if str(item["group_id"]) == group_id),
            None,
        )
        verified_until = (_utc_now() + timedelta(minutes=10)).isoformat()
        if listed is not None:
            candidate = self.store.upsert_group_candidate(
                group_id,
                str(listed["group_name"]),
                source="forced_group_list",
                verification_status="verified",
                verification_method="group_list",
                verified_until=verified_until,
            )
            return {
                "status": "verified",
                "group_id": group_id,
                "group_name": candidate["group_name"],
                "verification_method": "group_list",
                "verified_until": verified_until,
            }

        try:
            async with self.manager.limiter:
                info = await self.client.get_group_info(group_id, no_cache=True)
            group_name = str(info.get("group_name") or group_id)
            verification_method: str | None = None
            member_error: Exception | None = None
            try:
                async with self.manager.limiter:
                    members = await self.client.get_group_member_list(group_id, no_cache=True)
                if any(
                    str(member.get("qq_user_id") or "") == self.config.account_id
                    for member in members
                ):
                    verification_method = "member_list"
                elif members:
                    self.store.upsert_group_candidate(
                        group_id,
                        group_name,
                        source="direct_probe",
                        verification_status="not_joined",
                        verification_method="member_list",
                        verified_until=None,
                    )
                    return {
                        "status": "not_joined",
                        "group_id": group_id,
                        "group_name": group_name,
                    }
            except Exception as error:
                member_error = error

            if verification_method is None:
                try:
                    async with self.manager.limiter:
                        await self.client.get_group_history(group_id, 1)
                    verification_method = "readable_history"
                except Exception as history_error:
                    selected_error = member_error or history_error
                    raise selected_error from history_error

            candidate = self.store.upsert_group_candidate(
                group_id,
                group_name,
                source="direct_probe",
                verification_status="verified",
                verification_method=verification_method,
                verified_until=verified_until,
            )
            return {
                "status": "group_registry_stale",
                "group_id": group_id,
                "group_name": candidate["group_name"],
                "verification_method": verification_method,
                "verified_until": verified_until,
                "registry_error": str(registry_error) if registry_error else None,
            }
        except (OneBotSessionError, AccountMismatchError) as error:
            self.manager.pause_session(error, source="probe_group")
            raise
        except OneBotConfigurationError as error:
            self.manager.pause_configuration(error, source="probe_group")
            raise
        except Exception as error:
            text = f"{type(error).__name__}: {error}"
            status = (
                "upstream_timeout"
                if any(word in text.lower() for word in ("timeout", "超时", "连接失败"))
                else "not_joined"
            )
            self.store.upsert_group_candidate(
                group_id,
                group_id,
                source="direct_probe",
                verification_status=status,
                verification_method=None,
                verified_until=None,
                error=text,
            )
            return {"status": status, "group_id": group_id, "error": text[:500]}

    async def get_status(self) -> dict[str, Any]:
        registry = self.store.runtime_status("group_registry")
        sse = self.store.runtime_status("sse")
        scheduler = self.store.runtime_status("sync_scheduler")
        control = self.manager.control_status()
        registry_ids = {str(item) for item in registry.get("group_ids", [])}
        sync_groups: list[dict[str, Any]] = []
        sync_degraded = False
        registry_suspect = False
        for group in self.store.list_groups():
            state = self.store.state(str(group["qq_group_id"]))
            freshness = sync_freshness(state, self.config.context_freshness_seconds)
            sync_degraded = sync_degraded or not freshness["fresh"]
            missing_from_registry = (
                bool(registry.get("ok")) and str(group["qq_group_id"]) not in registry_ids
            )
            registry_suspect = registry_suspect or missing_from_registry
            sync_groups.append(
                {
                    "group_key": group["group_key"],
                    "group_id": group["qq_group_id"],
                    "group_name": group["qq_group_name"],
                    "freshness": freshness,
                    "missing_from_group_list": missing_from_registry,
                }
            )
        registry_suspect = registry_suspect or any(
            (
                candidate["source"] in {"group_message_event", "group_increase_event"}
                or (candidate["source"] == "direct_probe" and candidate["verification_valid"])
            )
            and candidate["group_id"] not in registry_ids
            and candidate["available"]
            for candidate in self.store.list_group_candidates()
        )

        registry_error = str(registry.get("last_error") or "")
        scheduler_error = str(scheduler.get("last_error") or "")
        if control.get("status") == "paused_session":
            status = "login_required"
        elif control.get("status") == "paused_manual":
            status = "collection_paused"
        elif control.get("status") == "paused_configuration":
            status = "configuration_error"
        elif scheduler_error:
            status = (
                "login_required"
                if any(word in scheduler_error for word in ("未登录", "登录 QQ", "登录状态"))
                else "onebot_unreachable"
            )
        elif sync_degraded:
            status = "sync_degraded"
        elif registry_error or registry_suspect:
            status = "group_registry_suspect"
        else:
            status = "healthy"
        next_actions: list[dict[str, str]] = []
        if status in {"login_required", "onebot_unreachable"}:
            next_actions.append(
                {
                    "label": "打开 NapCat 面板",
                    "instruction": "调用 admin.open_napcat_webui，检查登录或扫码。",
                }
            )
        if status == "onebot_unreachable":
            next_actions.append(
                {
                    "label": "必要时恢复 NapCat",
                    "instruction": (
                        "仅当 NapCat 进程持续不可达且用户明确同意时调用 "
                        "admin.open_napcat_recovery；群列表缺失不能作为重启理由。"
                    ),
                }
            )
        if status in {"login_required", "collection_paused"}:
            next_actions.append(
                {
                    "label": "人工恢复采集",
                    "instruction": (
                        "完成 QQ 登录后，由用户明确要求调用 admin.resume_qq_collection。"
                    ),
                }
            )
        if status == "group_registry_suspect":
            next_actions.append(
                {
                    "label": "刷新或直接验证群",
                    "instruction": (
                        "先调用 admin.refresh_group_registry；若已知群号仍缺失，"
                        "调用 admin.probe_group。不要重启 NapCat。"
                    ),
                }
            )
        return {
            "status": status,
            "expected_account_id": self.config.account_id,
            "current_account_id": registry.get("account_id"),
            "onebot_reachable": not bool(scheduler_error),
            "collection_control": control,
            "sync_scheduler": scheduler,
            "group_registry": {
                **registry,
                "suspect": registry_suspect,
            },
            "sse": sse,
            "groups": sync_groups,
            "next_actions": next_actions,
        }

    async def handle_event(self, event: dict[str, Any]) -> None:
        if not self.manager.is_active():
            return
        self_id = str(event.get("self_id") or "")
        if self_id and self_id != self.config.account_id:
            self.manager.pause_session(
                AccountMismatchError(
                    f"SSE 事件来自 QQ {self_id}，配置要求 {self.config.account_id}"
                ),
                source="sse_event",
            )
            return
        post_type = str(event.get("post_type") or "")
        group_id = str(event.get("group_id") or "")
        if not group_id.isdigit():
            return
        group_name = str(event.get("group_name") or group_id)
        if post_type == "message" and str(event.get("message_type") or "") == "group":
            existing = self.store.get_group_by_qq(group_id)
            self.store.upsert_group_candidate(
                group_id,
                str(existing["qq_group_name"]) if existing else group_name,
                source="group_message_event",
            )
            if existing and existing["whitelisted"]:
                message = normalize_message(event, expected_group_id=group_id)
                if message is not None:
                    self.store.upsert([message])
            return
        if post_type != "notice":
            return
        notice_type = str(event.get("notice_type") or "")
        user_id = str(event.get("user_id") or "")
        if notice_type == "group_increase" and user_id == self.config.account_id:
            self.store.upsert_group_candidate(
                group_id,
                group_name,
                source="group_increase_event",
            )
        elif notice_type == "group_decrease" and user_id == self.config.account_id:
            self.store.mark_group_candidate_unavailable(group_id, source="group_decrease_event")

    async def run_sse_forever(self) -> None:
        delay = 1.0
        timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._onebot_token}"},
            timeout=timeout,
            transport=self._sse_transport,
        ) as client:
            while True:
                await self.manager.wait_until_active()
                try:
                    async with client.stream("GET", self.config.onebot_sse_url) as response:
                        if response.status_code in {401, 403}:
                            self.manager.pause_configuration(
                                OneBotConfigurationError("SSE OneBot Token 或访问控制配置错误"),
                                source="sse",
                            )
                            raise CollectionPausedError("SSE 配置熔断")
                        response.raise_for_status()
                        self.store.set_runtime_status(
                            "sse",
                            {
                                "connected": True,
                                "connected_at": _iso_now(),
                                "last_event_at": self.store.runtime_status("sse").get(
                                    "last_event_at"
                                ),
                                "last_error": None,
                            },
                        )
                        delay = 1.0
                        data: list[str] = []
                        async for line in response.aiter_lines():
                            if not self.manager.is_active():
                                raise CollectionPausedError("QQ 采集已暂停")
                            if line.startswith("data:"):
                                data.append(line[5:].lstrip())
                                continue
                            if line or not data:
                                continue
                            raw = "\n".join(data)
                            data.clear()
                            try:
                                event = json.loads(raw)
                                if isinstance(event, dict):
                                    await self.handle_event(event)
                                    self.store.set_runtime_status(
                                        "sse",
                                        {
                                            "connected": True,
                                            "connected_at": self.store.runtime_status("sse").get(
                                                "connected_at"
                                            ),
                                            "last_event_at": _iso_now(),
                                            "last_error": None,
                                        },
                                    )
                            except (ValueError, json.JSONDecodeError) as error:
                                LOGGER.warning("忽略无效 OneBot SSE 事件：%s", error)
                    raise RuntimeError("SSE 连接已结束")
                except asyncio.CancelledError:
                    raise
                except CollectionPausedError:
                    self.store.set_runtime_status(
                        "sse",
                        {
                            "connected": False,
                            "connected_at": self.store.runtime_status("sse").get("connected_at"),
                            "last_event_at": self.store.runtime_status("sse").get("last_event_at"),
                            "last_error": "collection_paused",
                        },
                    )
                    continue
                except Exception as error:
                    self.store.set_runtime_status(
                        "sse",
                        {
                            "connected": False,
                            "connected_at": self.store.runtime_status("sse").get("connected_at"),
                            "last_event_at": self.store.runtime_status("sse").get("last_event_at"),
                            "last_error": f"{type(error).__name__}: {error}"[:500],
                        },
                    )
                    LOGGER.warning("NapCat SSE 连接失败，%.0f 秒后重试：%s", delay, error)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

    async def run_discovery_forever(self) -> None:
        while True:
            await self.manager.wait_until_active()
            try:
                await self.refresh_registry()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("主动刷新群列表失败：%s", error)
            await asyncio.sleep(self.config.group_discovery_interval_seconds)

    def request_restart(self) -> Path:
        directory = self.config.napcat_control_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        request = directory / "restart-napcat.request"
        if request.exists():
            raise RuntimeError("NapCat 重启请求正在处理中")
        temporary = directory / ".restart-napcat.request.tmp"
        temporary.write_text(
            json.dumps(
                {"requested_at": _iso_now(), "account_id": self.config.account_id},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(request)
        return request

    def restart_status(self) -> dict[str, Any]:
        path = self.config.napcat_control_dir / "restart-napcat.status.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"status": "never_requested"}
        except (OSError, json.JSONDecodeError) as error:
            return {"status": "unreadable", "error": str(error)}
        return value if isinstance(value, dict) else {"status": "invalid"}
