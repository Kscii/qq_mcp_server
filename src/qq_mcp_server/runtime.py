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
    onebot_action_source,
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
        self._session_id: str | None = None
        self.store.ensure_active_qq_account(config.account_id)
        abandoned = self.store.close_abandoned_collector_session()
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
        sse = self.store.runtime_status("sse")
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
        elif not sse.get("connected"):
            status = "onebot_unreachable" if sse.get("last_error") else "sse_connecting"
        elif sse.get("online") is False or sse.get("good") is False:
            status = "login_required"
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
            "onebot_reachable": bool(sse.get("connected")),
            "collection_control": control,
            "group_registry": registry,
            "sse": sse,
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
        self.store.close_open_message_gaps(
            end_at=event_at or int(_utc_now().timestamp()),
            automatic_only=True,
        )

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
                current = self.store.runtime_status("sse")
                self.store.set_runtime_status(
                    "sse",
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

    async def run_watchdog_forever(self) -> None:
        while True:
            now = _utc_now()
            sse = self.store.runtime_status("sse")
            heartbeat_at = sse.get("last_heartbeat_at")
            heartbeat_age: float | None = None
            if isinstance(heartbeat_at, str):
                try:
                    heartbeat_age = max(
                        0.0, (now - datetime.fromisoformat(heartbeat_at)).total_seconds()
                    )
                except ValueError:
                    heartbeat_age = None
            interval_ms = int(sse.get("heartbeat_interval_ms") or 30_000)
            threshold = max(60.0, interval_ms * 3 / 1000)
            # Some NapCat builds never emit heartbeat meta events. In that case
            # the open SSE transport, rather than an absent heartbeat, is the
            # only available health signal.
            heartbeat_observed = heartbeat_age is not None
            stale = bool(
                self.manager.is_active()
                and sse.get("connected")
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
                },
            )
            await asyncio.sleep(10)

    def pause_collection(self, reason: str, *, source: str = "admin_mcp") -> dict[str, Any]:
        self._open_outage(
            source="collection_pause" if source == "admin_mcp" else source,
            confidence="confirmed",
        )
        if source == "admin_mcp":
            return self.manager.pause_manual(reason)
        return self.manager.pause_for(reason, source=source)

    async def resume_collection(self) -> dict[str, Any]:
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
