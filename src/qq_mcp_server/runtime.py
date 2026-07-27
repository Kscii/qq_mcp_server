from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
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
    OneBotTransportError,
    onebot_action_source,
)
from qq_mcp_server.store import MessageStore
from qq_mcp_server.sync import (
    AccountMismatchError,
    CollectionPausedError,
    MultiGroupSyncManager,
    SyncService,
)

LOGGER = logging.getLogger(__name__)
STATUS_CHECK_INTERVAL_SECONDS = 60
RECOVERY_QUIET_SECONDS = 300
EXPLICIT_HISTORY_COOLDOWN_SECONDS = 600
REGISTRY_COOLDOWN_SECONDS = 3600
MEMBER_LIST_COOLDOWN_SECONDS = 3600


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
    """群发现、被动事件导入、会话熔断和面向 MCP 的诊断状态。"""

    def __init__(
        self,
        config: AppConfig,
        client: OneBotClient,
        store: MessageStore,
        onebot_token: str,
        manager: MultiGroupSyncManager | None = None,
        *,
        sse_transport: httpx.AsyncBaseTransport | None = None,
        collector_owner: bool = False,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self._onebot_token = onebot_token
        self.manager = manager or MultiGroupSyncManager(config, client, store)
        self._sse_transport = sse_transport
        self._registry_lock = asyncio.Lock()
        self._session_id: str | None = None
        self.store.ensure_active_qq_account(config.account_id)
        abandoned = self.store.close_abandoned_collector_session() if collector_owner else None
        if abandoned is not None:
            heartbeat_at = abandoned.get("last_heartbeat_at") or abandoned.get("connected_at")
            try:
                start_at = int(datetime.fromisoformat(str(heartbeat_at)).timestamp())
            except (TypeError, ValueError):
                start_at = int(_utc_now().timestamp())
            self.store.create_message_gaps_for_all(
                start_at=start_at,
                confidence="suspected",
                source="unclean_restart",
            )

    def _transport_status(self) -> dict[str, Any]:
        status = self.store.runtime_status("event_transport")
        if status.get("updated_at") is not None:
            return status
        return self.store.runtime_status("sse")

    def _set_transport_status(self, value: dict[str, Any]) -> None:
        # 保留 sse 兼容键，避免升级瞬间让旧客户端和只读仪表盘失去状态。
        self.store.set_runtime_status("event_transport", value)
        self.store.set_runtime_status("sse", value)

    def health_snapshot(self) -> dict[str, Any]:
        transport = self._transport_status()
        session = self.store.runtime_status("session_health")
        control = self.manager.control_status()
        interval_ms = int(transport.get("heartbeat_interval_ms") or 30_000)
        timeout_seconds = max(60.0, interval_ms * 3 / 1000)
        event_age = _age_seconds(
            transport.get("last_heartbeat_at") or transport.get("last_event_at")
        )
        event_fresh = bool(
            transport.get("connected")
            and (
                (event_age is not None and event_age <= timeout_seconds)
                or (event_age is None and transport.get("transport") != "reverse_websocket")
            )
        )
        qq_online = session.get("qq_online")
        if not isinstance(qq_online, bool):
            candidate = transport.get("online")
            qq_online = candidate if isinstance(candidate, bool) else False
        safe = bool(
            control.get("status") == "active"
            and session.get("recovery_state") in {None, "active"}
            and qq_online
            and event_fresh
        )
        return {
            "qq_online": qq_online,
            "onebot_reachable": bool(
                session.get("onebot_reachable")
                if session.get("updated_at") is not None
                else transport.get("connected")
            ),
            "event_connected": bool(transport.get("connected")),
            "data_fresh": bool(qq_online and event_fresh),
            "safe_to_roleplay": safe,
            "last_event_at": transport.get("last_event_at"),
            "last_heartbeat_at": transport.get("last_heartbeat_at"),
            "event_age_seconds": round(event_age, 3) if event_age is not None else None,
            "event_timeout_seconds": timeout_seconds,
            "offline_since": session.get("offline_since"),
            "online_since": session.get("online_since"),
            "recovery_state": session.get("recovery_state") or ("active" if safe else "unknown"),
            "offline_reason": session.get("offline_reason"),
            "last_status_check_at": session.get("last_status_check_at"),
            "consecutive_online_checks": int(session.get("consecutive_online_checks") or 0),
            "collection_control": control,
            "event_transport": transport,
        }

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
                        age = REGISTRY_COOLDOWN_SECONDS
                    if age < REGISTRY_COOLDOWN_SECONDS:
                        raise RuntimeError(
                            "群列表强制刷新冷却中，请等待 "
                            f"{max(1, int(REGISTRY_COOLDOWN_SECONDS - age))} 秒"
                        )
            attempt_at = _iso_now()
            try:
                async with self.manager.limiter:
                    with onebot_action_source(self.client, "manual_group_registry_refresh"):
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
        observed = self.store.group_candidate(group_id)
        if (
            observed
            and observed["available"]
            and observed["source"] in {"group_message_event", "group_increase_event"}
        ):
            return {
                "status": "verified",
                "group_id": group_id,
                "group_name": observed["group_name"],
                "verification_method": "sse_event",
                "verified_until": None,
            }
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
                with onebot_action_source(self.client, "manual_group_probe"):
                    info = await self.client.get_group_info(group_id, no_cache=True)
            group_name = str(info.get("group_name") or group_id)
            verification_method: str | None = None
            member_error: Exception | None = None
            try:
                async with self.manager.limiter:
                    with onebot_action_source(self.client, "manual_group_probe"):
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
                if member_error is not None:
                    raise member_error
                raise ValueError("无法通过群列表、SSE 事件或成员列表验证当前账号在群内")

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
        health = self.health_snapshot()
        transport = health["event_transport"]
        control = self.manager.control_status()
        unresolved_gaps = self.store.list_message_gaps(unresolved_only=True)
        group_status: list[dict[str, Any]] = []
        for group in self.store.list_groups():
            state = self.store.state(str(group["qq_group_id"]))
            gaps = [
                gap for gap in unresolved_gaps if str(gap["group_id"]) == str(group["qq_group_id"])
            ]
            group_status.append(
                {
                    "group_key": group["group_key"],
                    "group_id": group["qq_group_id"],
                    "group_name": group["qq_group_name"],
                    "ai_access_enabled": bool(group["whitelisted"]),
                    "message_count": state["message_count"],
                    "newest_message_at": state["newest_time"],
                    "unresolved_gap_count": len(gaps),
                }
            )
        if control.get("status") == "paused_session":
            status = "login_required"
        elif control.get("status") == "paused_manual":
            status = "collection_paused"
        elif control.get("status") == "paused_configuration":
            status = "configuration_error"
        elif health["onebot_reachable"] is False:
            status = (
                "onebot_unreachable"
                if health.get("last_status_check_at") or transport.get("last_error")
                else "status_check_pending"
            )
        elif health["qq_online"] is False:
            status = "login_required"
        elif not health["event_connected"]:
            status = "event_connecting"
        elif not health["data_fresh"]:
            status = "event_stale"
        elif unresolved_gaps:
            status = "data_gap_warning"
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
        if unresolved_gaps:
            next_actions.append(
                {
                    "label": "检查消息缺口",
                    "instruction": "调用 admin.list_message_gaps；只在用户确认后启动区间修复。",
                }
            )
        return {
            "status": status,
            "expected_account_id": self.config.account_id,
            "current_account_id": (self.store.active_qq_account() or {}).get("account_id"),
            **{
                key: value
                for key, value in health.items()
                if key not in {"collection_control", "event_transport"}
            },
            "collection_control": control,
            "group_registry": registry,
            "event_transport": transport,
            "sse": transport,
            "collector_session": self.store.active_collector_session(),
            "accounts": self.store.list_qq_accounts(),
            "latest_account_switch": self.store.latest_qq_account_switch(),
            "onebot_action_audit": self.store.onebot_action_summary(),
            "unresolved_message_gaps": unresolved_gaps,
            "groups": group_status,
            "next_actions": next_actions,
        }

    def _open_outage(self, *, source: str, confidence: str, start_at: int | None = None) -> None:
        self.store.create_message_gaps_for_all(
            start_at=start_at or int(_utc_now().timestamp()),
            confidence=confidence,
            source=source,
        )

    def _mark_stream_healthy(self, *, event_at: int | None = None) -> None:
        if not self.manager.is_active():
            return
        self.store.close_open_message_gaps(
            end_at=event_at or int(_utc_now().timestamp()),
            automatic_only=True,
        )

    async def handle_event(self, event: dict[str, Any]) -> None:
        if not self.manager.allows_passive_events():
            return
        self_id = str(event.get("self_id") or "")
        if self_id and self_id != self.config.account_id:
            self.manager.pause_session(
                AccountMismatchError(f"事件来自 QQ {self_id}，配置要求 {self.config.account_id}"),
                source="event_transport",
            )
            return
        post_type = str(event.get("post_type") or "")
        event_at = int(event.get("time") or int(_utc_now().timestamp()))
        if post_type == "meta_event":
            meta_type = str(event.get("meta_event_type") or "")
            if meta_type == "heartbeat":
                status = event.get("status")
                status = status if isinstance(status, dict) else {}
                online = status.get("online")
                good = status.get("good")
                interval = event.get("interval")
                interval_ms = int(interval) if isinstance(interval, (int, float)) else None
                if self._session_id is not None:
                    with suppress(KeyError):
                        self.store.update_collector_heartbeat(
                            self._session_id,
                            interval_ms=interval_ms,
                            online=online if isinstance(online, bool) else None,
                            good=good if isinstance(good, bool) else None,
                        )
                current = self._transport_status()
                self._set_transport_status(
                    {
                        **{key: value for key, value in current.items() if key != "updated_at"},
                        "connected": True,
                        "last_event_at": _iso_now(),
                        "last_heartbeat_at": _iso_now(),
                        "heartbeat_interval_ms": interval_ms,
                        "online": online if isinstance(online, bool) else None,
                        "good": good if isinstance(good, bool) else None,
                        "last_error": None,
                    },
                )
                if online is False or good is False:
                    self._open_outage(
                        source="heartbeat_degraded",
                        confidence="confirmed",
                        start_at=event_at,
                    )
                    if self.manager.is_active():
                        self.manager.pause_session(
                            OneBotSessionError("NapCat 心跳报告 QQ 已离线"),
                            source="event_heartbeat",
                        )
                    current_health = self.store.runtime_status("session_health")
                    self.store.set_runtime_status(
                        "session_health",
                        {
                            **{
                                key: value
                                for key, value in current_health.items()
                                if key != "updated_at"
                            },
                            "qq_online": False,
                            "onebot_reachable": True,
                            "offline_since": current_health.get("offline_since") or _iso_now(),
                            "online_since": None,
                            "consecutive_online_checks": 0,
                            "recovery_state": "offline",
                            "offline_reason": "NapCat 心跳报告 QQ 已离线",
                            "last_status_check_at": current_health.get("last_status_check_at"),
                        },
                    )
                else:
                    self._mark_stream_healthy(event_at=event_at)
            elif meta_type == "lifecycle":
                self._mark_stream_healthy(event_at=event_at)
            return
        group_id = str(event.get("group_id") or "")
        if not group_id.isdigit():
            return
        group_name = str(event.get("group_name") or group_id)
        if (
            post_type in {"message", "message_sent"}
            and str(event.get("message_type") or "") == "group"
        ):
            existing = self.store.get_group_by_qq(group_id)
            self.store.upsert_group_candidate(
                group_id,
                str(existing["qq_group_name"]) if existing else group_name,
                source="group_message_event",
            )
            message = normalize_message(event, expected_group_id=group_id)
            if message is not None:
                self.store.upsert([message])
            self._mark_stream_healthy(event_at=event_at)
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

    def _begin_sse_session(self) -> None:
        session = self.store.start_collector_session(self.config.account_id)
        self._session_id = str(session["session_id"])

    def _end_sse_session(
        self, *, reason: str, open_gap: bool, confidence: str = "confirmed"
    ) -> None:
        if self._session_id is not None:
            with suppress(KeyError):
                self.store.end_collector_session(self._session_id, reason=reason)
            self._session_id = None
        if open_gap:
            self._open_outage(source="sse_disconnect", confidence=confidence)

    def begin_event_session(self) -> str:
        if self._session_id is not None:
            self.end_event_session(
                reason="replaced_by_new_connection",
                open_gap=False,
            )
        self._begin_sse_session()
        previous = self._transport_status()
        self._set_transport_status(
            {
                "transport": "reverse_websocket",
                "connected": True,
                "connected_at": _iso_now(),
                "last_event_at": previous.get("last_event_at"),
                "last_heartbeat_at": previous.get("last_heartbeat_at"),
                "heartbeat_interval_ms": previous.get("heartbeat_interval_ms"),
                "online": previous.get("online"),
                "good": previous.get("good"),
                "last_error": None,
            }
        )
        assert self._session_id is not None
        return self._session_id

    def record_event_received(self) -> None:
        current = self._transport_status()
        self._set_transport_status(
            {
                **{key: value for key, value in current.items() if key != "updated_at"},
                "transport": "reverse_websocket",
                "connected": True,
                "last_event_at": _iso_now(),
                "last_error": None,
            }
        )

    def end_event_session(
        self,
        *,
        reason: str,
        open_gap: bool = True,
        confidence: str = "confirmed",
        session_id: str | None = None,
    ) -> None:
        if session_id is not None and session_id != self._session_id:
            with suppress(KeyError):
                self.store.end_collector_session(session_id, reason=reason)
            return
        if self._session_id is not None:
            with suppress(KeyError):
                self.store.end_collector_session(self._session_id, reason=reason)
            self._session_id = None
        if open_gap:
            self._open_outage(
                source="event_disconnect",
                confidence=confidence,
            )
        previous = self._transport_status()
        self._set_transport_status(
            {
                **{key: value for key, value in previous.items() if key != "updated_at"},
                "transport": "reverse_websocket",
                "connected": False,
                "last_error": reason[:500],
            }
        )

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
                        self._begin_sse_session()
                        previous = self.store.runtime_status("sse")
                        self.store.set_runtime_status(
                            "sse",
                            {
                                "connected": True,
                                "connected_at": _iso_now(),
                                "last_event_at": previous.get("last_event_at"),
                                "last_heartbeat_at": previous.get("last_heartbeat_at"),
                                "heartbeat_interval_ms": previous.get("heartbeat_interval_ms"),
                                "online": previous.get("online"),
                                "good": previous.get("good"),
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
                                    current = self.store.runtime_status("sse")
                                    self.store.set_runtime_status(
                                        "sse",
                                        {
                                            **{
                                                key: value
                                                for key, value in current.items()
                                                if key != "updated_at"
                                            },
                                            "connected": True,
                                            "last_event_at": _iso_now(),
                                            "last_error": None,
                                        },
                                    )
                            except (ValueError, json.JSONDecodeError) as error:
                                LOGGER.warning("忽略无效 OneBot SSE 事件：%s", error)
                    raise RuntimeError("SSE 连接已结束")
                except asyncio.CancelledError:
                    self._end_sse_session(
                        reason="application_shutdown",
                        open_gap=True,
                        confidence="suspected",
                    )
                    raise
                except CollectionPausedError:
                    self._end_sse_session(
                        reason="collection_paused",
                        open_gap=True,
                        confidence="confirmed",
                    )
                    previous = self.store.runtime_status("sse")
                    self.store.set_runtime_status(
                        "sse",
                        {
                            **{
                                key: value for key, value in previous.items() if key != "updated_at"
                            },
                            "connected": False,
                            "last_error": "collection_paused",
                        },
                    )
                    continue
                except Exception as error:
                    self._end_sse_session(
                        reason=f"{type(error).__name__}: {error}"[:500],
                        open_gap=True,
                    )
                    previous = self.store.runtime_status("sse")
                    self.store.set_runtime_status(
                        "sse",
                        {
                            **{
                                key: value for key, value in previous.items() if key != "updated_at"
                            },
                            "connected": False,
                            "last_error": f"{type(error).__name__}: {error}"[:500],
                        },
                    )
                    LOGGER.warning("NapCat SSE 连接失败，%.0f 秒后重试：%s", delay, error)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

    async def run_discovery_forever(self) -> None:
        """兼容旧调用；新应用启动链不会运行周期群发现。"""
        while True:
            await self.manager.wait_until_active()
            try:
                await self.refresh_registry()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("主动刷新群列表失败：%s", error)
            await asyncio.sleep(self.config.group_discovery_interval_seconds)

    def _write_session_health(self, **updates: Any) -> dict[str, Any]:
        previous = self.store.runtime_status("session_health")
        value = {key: value for key, value in previous.items() if key != "updated_at"}
        value.update(updates)
        self.store.set_runtime_status("session_health", value)
        return self.store.runtime_status("session_health")

    async def _automatic_history_recovery(
        self,
        gaps: list[dict[str, Any]],
        *,
        recovery_key: str,
    ) -> dict[str, Any]:
        enabled = [group for group in self.store.list_groups() if group["roleplay_enabled"]]
        if not enabled:
            return {"status": "skipped", "reason": "没有启用的跑团群"}
        if self.store.history_actions_in_last_24_hours() >= 30:
            return {"status": "skipped", "reason": "历史请求已达到 24 小时安全上限"}
        group = enabled[0]
        group_id = str(group["qq_group_id"])
        target = next(
            (item for item in self.store.sync_targets() if item.group_id == group_id),
            None,
        )
        if target is None:
            return {"status": "skipped", "reason": "找不到同步目标"}
        source = f"automatic_login_recovery:{group_id}"
        self._write_session_health(
            recovery_history_attempted_for=recovery_key,
            recovery_history_attempted_at=_iso_now(),
        )
        try:
            service = SyncService(
                self.config,
                target,
                self.client,
                self.store,
                self.manager.limiter,
            )
            with onebot_action_source(self.client, source):
                result = await service.sync_recent_page()
            repaired: list[str] = []
            unresolved: list[str] = []
            for original in gaps:
                if str(original["group_id"]) != group_id:
                    continue
                refreshed = self.store.refresh_message_gap_boundaries(str(original["gap_id"]))
                has_before_boundary = bool(refreshed.get("before_message_id"))
                if result.boundary_found and has_before_boundary:
                    self.store.update_message_gap_repair(
                        str(refreshed["gap_id"]),
                        status="repaired",
                        increment_pages=True,
                    )
                    repaired.append(str(refreshed["gap_id"]))
                else:
                    self.store.update_message_gap_repair(
                        str(refreshed["gap_id"]),
                        status="paused",
                        increment_pages=True,
                        error="登录恢复只允许单页补偿，未验证已知边界",
                    )
                    unresolved.append(str(refreshed["gap_id"]))
            return {
                "status": "completed",
                "group_id": group_id,
                "received": result.received,
                "inserted": result.inserted,
                "boundary_found": result.boundary_found,
                "repaired_gap_ids": repaired,
                "unresolved_gap_ids": unresolved,
            }
        except Exception as error:
            for original in gaps:
                if str(original["group_id"]) == group_id:
                    self.store.update_message_gap_repair(
                        str(original["gap_id"]),
                        status="paused",
                        increment_pages=True,
                        error=f"自动单页补偿失败：{type(error).__name__}: {error}",
                    )
            return {
                "status": "failed",
                "group_id": group_id,
                "error": f"{type(error).__name__}: {error}"[:500],
            }

    async def _check_session_status(self, now: datetime) -> None:
        current = self.store.runtime_status("session_health")
        checked_at = now.isoformat()
        try:
            with onebot_action_source(self.client, "session_watchdog"):
                status = await self.client.get_status()
        except OneBotConfigurationError as error:
            if self.manager.is_active():
                self.manager.pause_configuration(error, source="session_watchdog")
            self._open_outage(
                source="onebot_unreachable",
                confidence="confirmed",
                start_at=int(now.timestamp()),
            )
            self._write_session_health(
                qq_online=False,
                onebot_reachable=False,
                offline_since=current.get("offline_since") or checked_at,
                online_since=None,
                consecutive_online_checks=0,
                recovery_state="configuration_error",
                offline_reason=str(error)[:500],
                last_status_check_at=checked_at,
            )
            return
        except (OneBotTransportError, OSError, httpx.HTTPError) as error:
            if self.manager.is_active():
                self.manager.pause_session(
                    OneBotSessionError("NapCat 本地接口不可达"),
                    source="session_watchdog",
                )
            self._open_outage(
                source="onebot_unreachable",
                confidence="confirmed",
                start_at=int(now.timestamp()),
            )
            self._write_session_health(
                qq_online=False,
                onebot_reachable=False,
                offline_since=current.get("offline_since") or checked_at,
                online_since=None,
                consecutive_online_checks=0,
                recovery_state="offline",
                offline_reason=f"{type(error).__name__}: {error}"[:500],
                last_status_check_at=checked_at,
            )
            return

        online = bool(status.get("online"))
        if not online:
            if self.manager.is_active():
                self.manager.pause_session(
                    OneBotSessionError("NapCat 本地状态报告 QQ 已离线"),
                    source="session_watchdog",
                )
            self._open_outage(
                source="session_offline",
                confidence="confirmed",
                start_at=int(now.timestamp()),
            )
            self._write_session_health(
                qq_online=False,
                onebot_reachable=True,
                offline_since=current.get("offline_since") or checked_at,
                online_since=None,
                consecutive_online_checks=0,
                recovery_state="offline",
                offline_reason="NapCat 本地状态报告 QQ 已离线",
                last_status_check_at=checked_at,
            )
            return

        control = self.store.runtime_status("collection_control")
        if control.get("status") in {"paused_manual", "paused_configuration"}:
            self._write_session_health(
                qq_online=True,
                onebot_reachable=True,
                online_since=current.get("online_since") or checked_at,
                consecutive_online_checks=int(current.get("consecutive_online_checks") or 0) + 1,
                recovery_state=str(control.get("status")),
                offline_reason=control.get("reason"),
                last_status_check_at=checked_at,
            )
            return

        if control.get("status") != "paused_session":
            self._write_session_health(
                qq_online=True,
                onebot_reachable=True,
                offline_since=None,
                online_since=current.get("online_since") or checked_at,
                consecutive_online_checks=int(current.get("consecutive_online_checks") or 0) + 1,
                recovery_state="active",
                offline_reason=None,
                last_status_check_at=checked_at,
            )
            return

        online_since_text = current.get("online_since")
        try:
            online_since = (
                datetime.fromisoformat(str(online_since_text)) if online_since_text else now
            )
        except ValueError:
            online_since = now
        consecutive = int(current.get("consecutive_online_checks") or 0) + 1
        quiet_elapsed = max(0.0, (now - online_since).total_seconds())
        self._write_session_health(
            qq_online=True,
            onebot_reachable=True,
            online_since=online_since.isoformat(),
            consecutive_online_checks=consecutive,
            recovery_state="stabilizing",
            offline_reason=control.get("reason"),
            last_status_check_at=checked_at,
        )
        if consecutive < 2 or quiet_elapsed < RECOVERY_QUIET_SECONDS:
            return

        try:
            async with self.manager.limiter:
                with onebot_action_source(self.client, "automatic_login_verification"):
                    login = await self.client.get_login_info()
            actual = str(login.get("user_id") or "")
            if actual != self.config.account_id:
                raise AccountMismatchError(
                    f"NapCat 当前登录 QQ {actual or '未知'}，配置要求 {self.config.account_id}"
                )
        except Exception as error:
            self.manager.pause_session(error, source="automatic_login_verification")
            self._write_session_health(
                recovery_state="verification_failed",
                offline_reason=f"{type(error).__name__}: {error}"[:500],
            )
            return

        recovery_key = str(current.get("offline_since") or checked_at)
        self.manager.activate_verified(source="automatic_login_recovery")
        gaps = self.store.close_open_message_gaps(
            end_at=int(now.timestamp()),
            automatic_only=True,
        )
        history = await self._automatic_history_recovery(
            gaps,
            recovery_key=recovery_key,
        )
        self._write_session_health(
            qq_online=True,
            onebot_reachable=True,
            offline_since=None,
            recovery_state="active",
            offline_reason=None,
            recovered_at=_iso_now(),
            recovery_history=history,
        )

    async def refresh_recent_messages(self, group_id: str) -> dict[str, Any]:
        self.manager.require_active()
        health = self.health_snapshot()
        if not health["safe_to_roleplay"]:
            raise CollectionPausedError("QQ 会话或事件链路尚未达到安全就绪状态")
        source = f"explicit_recent_refresh:{group_id}"
        cooldown = self.store.onebot_action_cooldown(
            "get_group_msg_history",
            source,
            cooldown_seconds=EXPLICIT_HISTORY_COOLDOWN_SECONDS,
        )
        if not cooldown["allowed"]:
            raise RuntimeError(f"该群历史刷新冷却中，请等待 {cooldown['remaining_seconds']} 秒")
        if self.store.history_actions_in_last_24_hours() >= 30:
            raise RuntimeError("过去 24 小时历史请求已达到 30 页安全上限")
        target = next(
            (item for item in self.store.sync_targets() if item.group_id == group_id),
            None,
        )
        if target is None:
            raise KeyError("群尚未授权 AI 访问")
        service = SyncService(
            self.config,
            target,
            self.client,
            self.store,
            self.manager.limiter,
        )
        try:
            with onebot_action_source(self.client, source):
                result = await service.sync_recent_page()
        except OneBotSessionError as error:
            self.manager.pause_session(error, source="explicit_recent_refresh")
            raise
        return {
            "status": "completed",
            "received": result.received,
            "inserted": result.inserted,
            "boundary_found": result.boundary_found,
            "complete": result.complete,
            "cooldown_seconds": EXPLICIT_HISTORY_COOLDOWN_SECONDS,
        }

    async def run_watchdog_forever(self) -> None:
        while True:
            now = _utc_now()
            transport = self._transport_status()
            heartbeat_age = _age_seconds(transport.get("last_heartbeat_at"))
            interval_ms = int(transport.get("heartbeat_interval_ms") or 30_000)
            threshold = max(60.0, interval_ms * 3 / 1000)
            heartbeat_observed = heartbeat_age is not None
            stale = bool(
                transport.get("connected")
                and heartbeat_age is not None
                and heartbeat_age > threshold
            )
            if stale:
                self._open_outage(
                    source="heartbeat_timeout",
                    confidence="suspected",
                    start_at=int((now - timedelta(seconds=threshold)).timestamp()),
                )
            self.store.set_runtime_status(
                "collector_watchdog",
                {
                    "checked_at": now.isoformat(),
                    "heartbeat_age_seconds": (
                        round(heartbeat_age, 3) if heartbeat_age is not None else None
                    ),
                    "heartbeat_observed": heartbeat_observed,
                    "heartbeat_timeout_seconds": threshold,
                    "heartbeat_stale": stale,
                    "status_check_interval_seconds": STATUS_CHECK_INTERVAL_SECONDS,
                },
            )
            await self._check_session_status(now)
            await asyncio.sleep(STATUS_CHECK_INTERVAL_SECONDS)

    def pause_collection(self, reason: str, *, source: str = "admin_mcp") -> dict[str, Any]:
        self._open_outage(
            source="collection_pause" if source == "admin_mcp" else source,
            confidence="confirmed",
        )
        if source == "admin_mcp":
            return self.manager.pause_manual(reason)
        return self.manager.pause_for(reason, source=source)

    async def resume_collection(self) -> dict[str, Any]:
        control = self.store.runtime_status("collection_control")
        health = self.health_snapshot()
        if control.get("status") == "paused_session" and health.get("recovery_state") != "active":
            raise CollectionPausedError(
                "重新登录后仍处于五分钟稳定观察期；不能人工跳过会话安全检查"
            )
        return await self.manager.resume()

    def request_account_switch(self, switch_id: str) -> Path:
        switch = self.store.qq_account_switch(switch_id)
        if switch["status"] != "requested":
            raise ValueError("账号切换不是待确认状态")
        self.pause_collection(
            f"准备切换到 QQ {switch['target_account_id']}",
            source="account_switch",
        )
        directory = self.config.napcat_control_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        request = directory / "switch-napcat-account.request"
        if request.exists():
            raise RuntimeError("已有宿主机账号切换请求正在处理中")
        temporary = directory / ".switch-napcat-account.request.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "switch_id": switch_id,
                    "from_account_id": switch["from_account_id"],
                    "target_account_id": switch["target_account_id"],
                    "requested_at": _iso_now(),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(request)
        self.store.update_qq_account_switch(switch_id, status="host_pending")
        return request

    def account_switch_status(self, switch_id: str) -> dict[str, Any]:
        switch = self.store.qq_account_switch(switch_id)
        path = self.config.napcat_control_dir / "switch-napcat-account.status.json"
        try:
            host = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            host = {"status": "not_started"}
        except (OSError, json.JSONDecodeError) as error:
            host = {"status": "unreadable", "error": str(error)}
        if (
            switch["status"] == "host_pending"
            and isinstance(host, dict)
            and host.get("switch_id") == switch_id
            and host.get("status") == "awaiting_login"
        ):
            switch = self.store.update_qq_account_switch(switch_id, status="awaiting_login")
        return {"switch": switch, "host": host}

    async def complete_account_switch(self, switch_id: str) -> dict[str, Any]:
        state = self.account_switch_status(switch_id)
        switch = state["switch"]
        if switch["status"] not in {"host_pending", "awaiting_login"}:
            raise ValueError("账号切换当前不能完成验证")
        target = str(switch["target_account_id"])
        if self.config.account_id != target:
            raise RuntimeError("应用尚未由宿主机切换到目标账号配置")
        health = self.store.runtime_status("session_health")
        online_since = health.get("online_since")
        try:
            online_age = (
                (_utc_now() - datetime.fromisoformat(str(online_since))).total_seconds()
                if online_since
                else 0
            )
        except ValueError:
            online_age = 0
        if (
            health.get("qq_online") is not True
            or int(health.get("consecutive_online_checks") or 0) < 2
            or online_age < RECOVERY_QUIET_SECONDS
        ):
            remaining = max(1, int(RECOVERY_QUIET_SECONDS - online_age))
            raise RuntimeError(
                "目标 QQ 登录后需保持五分钟稳定，并由本地状态连续确认两次；"
                f"请约 {remaining} 秒后再完成切换"
            )
        async with self.manager.limiter:
            with onebot_action_source(self.client, "account_switch_finalize"):
                login = await self.client.get_login_info()
                actual = str(login.get("user_id") or "")
                if actual != target:
                    if actual:
                        self.store.update_qq_account_switch(
                            switch_id,
                            status="failed",
                            error=f"NapCat 登录的是 QQ {actual}，目标是 {target}",
                        )
                    raise AccountMismatchError(
                        f"NapCat 当前登录 QQ {actual or '未知'}，目标是 {target}"
                    )
                groups = await self.client.get_group_list()
        joined = {str(group["group_id"]) for group in groups}
        required = {
            str(group["qq_group_id"])
            for group in self.store.list_groups()
            if group["roleplay_enabled"]
        }
        missing = sorted(required - joined)
        if missing:
            self.store.update_qq_account_switch(
                switch_id,
                status="failed",
                error=f"目标账号缺少启用跑团群：{', '.join(missing)}",
            )
            raise ValueError("目标账号未加入全部启用跑团群：" + "、".join(missing))
        completed = self.store.update_qq_account_switch(switch_id, status="completed")
        control = self.manager.activate_verified(source="account_switch_finalize")
        return {
            "switch": completed,
            "collection_control": control,
            "verified_group_count": len(groups),
        }

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
