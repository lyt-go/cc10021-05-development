"""质量事件数据聚合服务：把事件、证据和调查结论聚合成每日根因矩阵和日报。

domain_chain: 质量事件数据聚合 (dq_incident_agg)
  └─ 聚合 → 质量事件 (dq_incident)
  └─ 派工 → 巡检任务 (dq_check_run)
  └─ 修复清单 → 质量报告 (dq_report)

和 `DqIncidentService.aggregate_incident_summary` 的口径差异：

* 旧摘要按“关联规则”累加，同一条异常被两个证据组跟进时会重复计数；
* 这里的计数主体是“事件编号”：同一事件不论有几组证据、几张派工单都只算一次；
* “根因待确认项”按“事件 + 证据组”的待核查单元单独核算，不并入对外事件总量；
* 调查结论带版本和归因依据快照：并发提交只落一份有效结论，更正保留当时的依据。

证据、派工单和调查结论保存在以下划线开头的内部明细表里，不会作为
业务模块出现在运营概览中；`dq_incident_agg` 本身只存每日日报快照。
"""

from __future__ import annotations

import copy
import threading
from datetime import date, datetime, timezone
from typing import Any, Iterable

from app.services.base import BaseModuleService
from app.store import store

# 资源路由每次请求都会新建一次服务实例，并发提交调查结论时只有模块级锁
# 才能保证“同一事件同一证据组只计入一次”。
_SUBMIT_LOCK = threading.RLock()

EVIDENCE_TABLE = "_incident_agg_evidence"
TASK_TABLE = "_incident_agg_task"
CONCLUSION_TABLE = "_incident_agg_conclusion"
MATRIX_TABLE = "_incident_agg_matrix"

DEFAULT_GROUPS = ("采集侧", "转换侧")
DEFAULT_ASSIGNEES = {"采集侧": "李采集", "转换侧": "王转换"}
OPEN_STATUSES = ("待确认", "调查中", "已修复")

CONFIRMED = "已确认"
PENDING = "待核查"

_REMEDIATION_BY_CAUSE = {
    "缺数": "补齐上游分区数据并回填下游表",
    "数据缺失": "补齐上游分区数据并回填下游表",
    "过滤条件变化": "修正 ETL 过滤条件并回溯重跑受影响分区",
    "过滤条件变更": "修正 ETL 过滤条件并回溯重跑受影响分区",
    "上游延迟": "上游补跑并延长下游等待窗口",
    "规则配置": "修订规则配置并在巡检任务中重新校验",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _utcnow().isoformat()


def _parse_day(value: Any) -> date:
    """把日期/日期字符串/None 统一成 date；None 取今天。"""
    if value is None or str(value).strip() == "":
        return _utcnow().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return _utcnow().date()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _next_id(table: str) -> int:
    rows = store.rows(table)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _event_key(row: dict[str, Any]) -> str:
    """事件身份：优先事件编号；缺少编号时退化成行 id，保证仍可聚合。"""
    return _text(row.get("事件编号")) or f"ID-{row.get('id')}"


class DqIncidentAggService(BaseModuleService):
    MODULE = "dq_incident_agg"
    ENTITY = "每日根因矩阵"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = [
        "报告编号", "报告日期", "事件总量", "根因待确认项",
        "已确认根因", "派工单数", "覆盖事件", "生成时间",
    ]
    REQUIRED_FIELDS = ["报告编号", "报告日期"]
    STATUS_ORDER = ["草稿", "已生成", "已归档"]
    ACTION_RULES = {"生成日报": "已生成", "归档日报": "已归档"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 基础读取
    # ------------------------------------------------------------------
    def _groups(self) -> list[str]:
        groups = list(DEFAULT_GROUPS)
        for table in (EVIDENCE_TABLE, TASK_TABLE, CONCLUSION_TABLE):
            for row in store.rows(table):
                group = _text(row.get("来源组"))
                if group and group not in groups:
                    groups.append(group)
        return groups

    def _scoped_events(self) -> list[dict[str, Any]]:
        """日报覆盖的事件：未关闭事件，按事件编号去重（同一条异常只算一条）。"""
        events: dict[str, dict[str, Any]] = {}
        for row in store.rows("dq_incident"):
            status = _text(row.get("status")) or _text(row.get("处理状态"))
            if status in ("已关闭", "关闭"):
                continue
            key = _event_key(row)
            # 重复事件编号只保留第一条，避免同一条异常被多条记录放大。
            events.setdefault(key, row)
        return list(events.values())

    def _event_rows(self, incident: str | int) -> list[dict[str, Any]]:
        key = str(incident).strip()
        matched = []
        for row in store.rows("dq_incident"):
            if key in (_event_key(row), _text(row.get("事件编号")), str(row.get("id"))):
                matched.append(row)
        return matched

    def _event_no(self, incident: str | int) -> str:
        rows = self._event_rows(incident)
        if not rows:
            return ""
        return _event_key(rows[0])

    def _active_conclusions(self, event_no: str) -> dict[str, dict[str, Any]]:
        active: dict[str, dict[str, Any]] = {}
        for row in store.rows(CONCLUSION_TABLE):
            if _text(row.get("事件编号")) != event_no:
                continue
            if not row.get("有效", False):
                continue
            active[_text(row.get("来源组"))] = row
        return active

    def _evidence_for(self, event_no: str, group: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(EVIDENCE_TABLE)
            if _text(row.get("事件编号")) == event_no and _text(row.get("来源组")) == group
        ]

    def _involved_groups(self, event_no: str) -> list[str]:
        """事件实际涉及的证据组：有证据、派工单或调查结论的组才算。"""
        groups = []
        for group in self._groups():
            if (
                self._evidence_for(event_no, group)
                or any(
                    _text(row.get("事件编号")) == event_no and _text(row.get("来源组")) == group
                    for row in store.rows(TASK_TABLE)
                )
                or any(
                    _text(row.get("事件编号")) == event_no and _text(row.get("来源组")) == group
                    for row in store.rows(CONCLUSION_TABLE)
                )
            ):
                groups.append(group)
        return groups

    def _task_for(self, event_no: str, group: str, report_date: str) -> dict[str, Any] | None:
        for row in store.rows(TASK_TABLE):
            if (
                _text(row.get("事件编号")) == event_no
                and _text(row.get("来源组")) == group
                and _text(row.get("报告日期")) == report_date
            ):
                return row
        return None

    # ------------------------------------------------------------------
    # 每日根因矩阵
    # ------------------------------------------------------------------
    def build_daily_root_cause_matrix(
        self,
        report_date: str | date | None = None,
        groups: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """生成每日根因矩阵：以事件为行、以证据组为列，列出各组待查对象。

        每个单元格代表“某事件在某证据组”的调查单元，包含待查对象、
        接手人、根因状态和该组已取得的证据。矩阵只读，不会写派工单；
        派工由 `dispatch_daily_investigations` 依据矩阵落单。
        """
        day = _parse_day(report_date)
        day_text = day.isoformat()
        group_names = [*groups] if groups else self._groups()
        events = self._scoped_events()

        matrix: list[dict[str, Any]] = []
        pending_items = 0
        confirmed_items = 0
        unassigned_objects: dict[str, list[str]] = {group: [] for group in group_names}
        pending_events: set[str] = set()
        events_without_evidence: list[str] = []

        for event in events:
            event_no = _event_key(event)
            active = self._active_conclusions(event_no)
            # 矩阵只呈现真正有人跟的组：有证据、派工单或结论。没有证据组的事件
            # 单独标记，不虚构两个组的待查工作量。
            row_groups = self._involved_groups(event_no)
            if not row_groups:
                events_without_evidence.append(event_no)
                # 还没有证据组的事件也要占一个根因待确认项：根因没人认领，
                # 是待分派对象而不是“已确认”。它只计入待确认口径，不放大事件总量。
                pending_items += 1
                pending_events.add(event_no)
                unassigned_objects.setdefault("未分组", []).append(
                    f"{_text(event.get('关联规则')) or event_no} 待登记证据")
                row_groups = []
            row = {
                "事件编号": event_no,
                "关联规则": _text(event.get("关联规则")),
                "严重级别": _text(event.get("严重级别")),
                "处理状态": _text(event.get("处理状态") or event.get("status")),
                "触发时间": _text(event.get("触发时间")),
                "分组": {},
                "关联根因": [],
                "尚无证据组": not row_groups,
                "事件计数": 1,
            }
            for group in row_groups:
                evidence = self._evidence_for(event_no, group)
                conclusion = active.get(group)
                # 优先当天的派工单；当天还没派工但历史上派过的，也把接手人带出来。
                task = self._task_for(event_no, group, day_text) or self._latest_task(event_no, group)
                target = (
                    _text((task or {}).get("待查对象"))
                    or next((_text(item.get("待查对象")) for item in evidence if _text(item.get("待查对象"))), "")
                    or f"{_text(event.get('关联规则')) or event_no} 的{group}证据"
                )
                assignee = (
                    _text((task or {}).get("接手人"))
                    or next((_text(item.get("取得人")) for item in evidence if _text(item.get("取得人"))), "")
                )
                status = CONFIRMED if conclusion else PENDING
                if status == CONFIRMED:
                    confirmed_items += 1
                else:
                    pending_items += 1
                    pending_events.add(event_no)
                    unassigned_objects.setdefault(group, []).append(target)
                cell = {
                    "来源组": group,
                    "待查对象": target,
                    "接手人": assignee,
                    "状态": status,
                    "根因": _text((conclusion or {}).get("根因")),
                    "结论编号": _text((conclusion or {}).get("结论编号")),
                    "归因依据": _text((conclusion or {}).get("归因依据")),
                    "证据条数": len(evidence),
                    "证据摘要": [_text(item.get("证据摘要")) for item in evidence if _text(item.get("证据摘要"))],
                    "派工单号": _text((task or {}).get("派工单号")),
                    "派工状态": _text((task or {}).get("状态")) or "未派工",
                    "关联根因": copy.deepcopy((conclusion or {}).get("关联根因", [])),
                }
                row["分组"][group] = cell
                for link in cell["关联根因"]:
                    suffix = f"{link.get('来源组')}「{link.get('根因')}」"
                    if suffix not in row["关联根因"]:
                        row["关联根因"].append(suffix)
            matrix.append(row)

        return {
            "报告日期": day_text,
            "统计范围": "未关闭事件（按事件编号去重）",
            "事件总量": len(matrix),
            "根因待确认项": pending_items,
            "已确认根因项": confirmed_items,
            "根因待确认事件数": len(pending_events),
            "尚无证据组事件": events_without_evidence,
            "分组待查": unassigned_objects,
            "矩阵": matrix,
            "核算口径": (
                "事件总量按事件编号去重；根因待确认项按“事件+证据组”计数，"
                "是调查工作量口径，不并入对外事件总量。"
            ),
            "生成时间": _now_text(),
        }

    # ------------------------------------------------------------------
    # 日报与派工
    # ------------------------------------------------------------------
    def dispatch_daily_investigations(
        self,
        report_date: str | date | None = None,
        assignees: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """依据每日根因矩阵派工：给每个待核查单元格落一张调查任务单。

        同一报告日期下重复执行是幂等的：已存在的“事件+证据组”任务
        只补接手人，不会重复生成派工单，也不会把单事件重复计入日报。
        """
        day_text = _parse_day(report_date).isoformat()
        matrix = self.build_daily_root_cause_matrix(day_text)
        assignees = assignees or {}
        created: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []

        with _SUBMIT_LOCK:
            for event_row in matrix["矩阵"]:
                event_no = event_row["事件编号"]
                for group, cell in event_row["分组"].items():
                    existing = self._task_for(event_no, group, day_text)
                    if existing is not None:
                        if assignees.get(group):
                            existing["接手人"] = assignees[group]
                        reused.append(self._task_view(existing))
                        continue
                    if cell["状态"] == CONFIRMED:
                        # 已有结论的调查单元不再派工，避免重复投入。
                        continue
                    task = {
                        "id": _next_id(TASK_TABLE),
                        "派工单号": "",
                        "报告日期": day_text,
                        "来源组": group,
                        "事件编号": event_no,
                        "待查对象": cell["待查对象"],
                        "接手人": assignees.get(group) or cell["接手人"] or DEFAULT_ASSIGNEES.get(group, f"{group}值班"),
                        "状态": PENDING,
                        "派工依据": "每日根因矩阵",
                        "矩阵日期": day_text,
                        "结论编号": "",
                        "根因": "",
                        "证据编号": [],
                        "派工时间": _now_text(),
                        "更新时间": _now_text(),
                    }
                    task["派工单号"] = f"INV-{day_text.replace('-', '')}-{task['id']:03d}"
                    store.rows(TASK_TABLE).append(task)
                    created.append(self._task_view(task))

            self._persist_matrix_snapshot(matrix)
            report = self._write_daily_report(matrix, day_text, created + reused)
            # 事件维度计数：日报按事件编号只计一次，派工单再多也不放大它。
            per_event: dict[str, dict[str, Any]] = {}
            for task in created + reused:
                entry = per_event.setdefault(task["事件编号"], {
                    "事件编号": task["事件编号"],
                    "事件计数": 1,
                    "派工单数": 0,
                    "派工组": [],
                })
                entry["派工单数"] += 1
                if task["来源组"] not in entry["派工组"]:
                    entry["派工组"].append(task["来源组"])

        return {
            "报告日期": day_text,
            "事件总量": matrix["事件总量"],
            "根因待确认项": matrix["根因待确认项"],
            "已确认根因项": matrix["已确认根因项"],
            "根因待确认事件数": matrix["根因待确认事件数"],
            "尚无证据组事件": matrix["尚无证据组事件"],
            "派工单": created + reused,
            "新增派工": created,
            "按组派工": {
                group: sum(1 for task in created + reused if task["来源组"] == group)
                for group in matrix["分组待查"]
            },
            "单事件计数": list(per_event.values()),
            "报告编号": report["报告编号"],
            "核算口径": matrix["核算口径"],
        }

    generate_daily_report = dispatch_daily_investigations

    def _persist_matrix_snapshot(self, matrix: dict[str, Any]) -> None:
        table = store.rows(MATRIX_TABLE)
        for event_row in matrix["矩阵"]:
            for group, cell in event_row["分组"].items():
                existing = next(
                    (
                        row for row in table
                        if _text(row.get("报告日期")) == matrix["报告日期"]
                        and _text(row.get("事件编号")) == event_row["事件编号"]
                        and _text(row.get("来源组")) == group
                    ),
                    None,
                )
                snapshot = {
                    "报告日期": matrix["报告日期"],
                    "事件编号": event_row["事件编号"],
                    "来源组": group,
                    "待查对象": cell["待查对象"],
                    "接手人": cell["接手人"],
                    "状态": cell["状态"],
                    "根因": cell["根因"],
                    "结论编号": cell["结论编号"],
                    "派工单号": cell["派工单号"],
                    "证据条数": cell["证据条数"],
                    "生成时间": matrix["生成时间"],
                }
                if existing is None:
                    snapshot["id"] = _next_id(MATRIX_TABLE)
                    table.append(snapshot)
                else:
                    existing.update(snapshot)

    def _write_daily_report(
        self,
        matrix: dict[str, Any],
        day_text: str,
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """写入一条日报快照记录，供质量报告和通用列表读取。"""
        rows = store.rows(self.MODULE)
        existing = next(
            (row for row in rows if _text(row.get("报告日期")) == day_text and _text(row.get("报告周期")) == "daily"),
            None,
        )
        if existing is None:
            new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            report = {
                "id": new_id,
                "报告编号": f"AGG-{day_text.replace('-', '')}-{new_id:02d}",
                "status": "已生成",
                "pending": False,
                "abnormal": matrix["根因待确认项"] > 0,
            }
            rows.append(report)
        else:
            report = existing

        report.update({
            "报告编号": report.get("报告编号") or f"AGG-{day_text.replace('-', '')}-{report['id']:02d}",
            "报告日期": day_text,
            "报告周期": "daily",
            "事件总量": str(matrix["事件总量"]),
            "根因待确认项": str(matrix["根因待确认项"]),
            "已确认根因": str(matrix["已确认根因项"]),
            "派工单数": str(len(tasks)),
            "覆盖事件": ",".join(row["事件编号"] for row in matrix["矩阵"]),
            "生成时间": matrix["生成时间"],
            "根因待确认事件数": matrix["根因待确认事件数"],
            "核算口径": matrix["核算口径"],
            "矩阵快照": copy.deepcopy(matrix["矩阵"]),
        })
        return report

    # ------------------------------------------------------------------
    # 调查证据
    # ------------------------------------------------------------------
    def record_investigation_evidence(
        self,
        incident: str | int,
        group: str,
        target: str,
        summary: str,
        evidence_type: str = "",
        obtained_by: str = "",
    ) -> dict[str, Any]:
        """登记某个证据组实际取得的调查证据。

        证据挂在“事件 + 证据组”上，矩阵据此判断哪些组真的在跟进；
        派工单只展示接手人本人取得的证据，避免把别组的发现算到自己头上。
        """
        rows = self._event_rows(incident)
        if not rows:
            return {"ok": False, "message": f"质量事件 {incident} 不存在"}
        event_no = _event_key(rows[0])
        group = _text(group)
        target = _text(target)
        summary = _text(summary)
        if not group or not target or not summary:
            return {"ok": False, "message": "证据组、待查对象和证据摘要不能为空"}

        evidence = {
            "id": _next_id(EVIDENCE_TABLE),
            "证据编号": "",
            "事件编号": event_no,
            "来源组": group,
            "待查对象": target,
            "证据摘要": summary,
            "证据类型": _text(evidence_type) or "现场证据",
            "取得人": _text(obtained_by) or DEFAULT_ASSIGNEES.get(group, group),
            "取得时间": _now_text(),
            "状态": "已取得",
        }
        evidence["证据编号"] = f"EV-{evidence['id']:03d}"
        store.rows(EVIDENCE_TABLE).append(evidence)
        return {"ok": True, "事件编号": event_no, "证据": copy.deepcopy(evidence)}

    # ------------------------------------------------------------------
    # 调查任务
    # ------------------------------------------------------------------
    def _task_view(self, task: dict[str, Any]) -> dict[str, Any]:
        """任务视图：只列出接手人本人实际取得的调查证据。"""
        event_no = _text(task.get("事件编号"))
        group = _text(task.get("来源组"))
        assignee = _text(task.get("接手人"))
        evidence = [
            row for row in self._evidence_for(event_no, group)
            if _text(row.get("取得人")) == assignee and _text(row.get("状态")) in ("", "已取得")
        ]
        view = copy.deepcopy(task)
        view["调查证据"] = [
            {
                "证据编号": item.get("证据编号") or f"EV-{item.get('id'):03d}",
                "证据类型": _text(item.get("证据类型")),
                "证据摘要": _text(item.get("证据摘要")),
                "取得人": _text(item.get("取得人")),
                "取得时间": _text(item.get("取得时间")),
            }
            for item in evidence
        ]
        view["证据条数"] = len(evidence)
        view["证据来源说明"] = f"仅列出接手人 {assignee or '（未指派）'} 本人取得的证据"
        return view

    def list_dispatched_tasks(
        self,
        group: str | None = None,
        report_date: str | date | None = None,
        incident: str | int | None = None,
        assignee: str | None = None,
    ) -> dict[str, Any]:
        """分别查看指定证据组发出的调查任务和接手人实际取得的证据。"""
        day_text = _parse_day(report_date).isoformat() if report_date else ""
        event_no = self._event_no(incident) if incident is not None else ""
        tasks = [
            self._task_view(row)
            for row in store.rows(TASK_TABLE)
            if (not group or _text(row.get("来源组")) == group)
            and (not day_text or _text(row.get("报告日期")) == day_text)
            and (not event_no or _text(row.get("事件编号")) == event_no)
            and (not assignee or _text(row.get("接手人")) == assignee)
        ]
        return {
            "来源组": group or "全部",
            "报告日期": day_text or "全部",
            "事件编号": event_no or "全部",
            "任务数": len(tasks),
            "调查证据总数": sum(task["证据条数"] for task in tasks),
            "调查任务": tasks,
        }

    # ------------------------------------------------------------------
    # 调查结论
    # ------------------------------------------------------------------
    def submit_investigation_conclusion(
        self,
        incident: str | int,
        group: str,
        root_cause: str,
        basis: str = "",
        evidence_ids: Iterable[int] | None = None,
        submitter: str | None = None,
        correction: bool = False,
        related_root_cause: str = "",
        report_date: str | date | None = None,
    ) -> dict[str, Any]:
        """提交调查结论：并发只计入一次，更正保留提交当时的归因依据。

        * 同一事件同一证据组同时只允许一份有效结论；重复/并发提交返回
          已有的有效结论（`计入=False`），让事件计数保持单事件口径。
        * `correction=True` 表示更正：旧结论标记为已更正但保留，新记录
          版本号 +1，并快照旧结论的归因依据（`更正前归因依据`）。
        """
        rows = self._event_rows(incident)
        if not rows:
            return {"ok": False, "计入": False, "message": f"质量事件 {incident} 不存在，无法提交调查结论"}
        event_no = _event_key(rows[0])
        group = _text(group)
        root_cause = _text(root_cause)
        if not group or not root_cause:
            return {"ok": False, "计入": False, "message": "证据组和根因不能为空"}

        day_text = _parse_day(report_date).isoformat()
        with _SUBMIT_LOCK:
            active = self._active_conclusions(event_no).get(group)
            if active is not None and not correction:
                return {
                    "ok": True,
                    "计入": False,
                    "重复提交": True,
                    "结论编号": _text(active.get("结论编号")),
                    "事件编号": event_no,
                    "来源组": group,
                    "版本": active.get("版本", 1),
                    "状态": _text(active.get("状态")),
                    "根因": _text(active.get("根因")),
                    "归因依据": _text(active.get("归因依据")),
                    "事件累计结论数": self._count_event_conclusions(),
                    "结论记录数": len(store.rows(CONCLUSION_TABLE)),
                    "message": "该事件该证据组已有有效调查结论，本次提交不重复计入；如需修改请显式更正",
                }

            version = int((active or {}).get("版本", 0)) + 1
            previous_basis = _text((active or {}).get("归因依据"))
            previous_root = _text((active or {}).get("根因"))
            previous_no = _text((active or {}).get("结论编号"))

            if evidence_ids is None:
                evidence = self._evidence_for(event_no, group)
                evidence_ids = [int(item["id"]) for item in evidence if item.get("id") is not None]
            evidence_ids = [int(item) for item in evidence_ids]
            evidence_texts = [
                _text(item.get("证据摘要"))
                for item in store.rows(EVIDENCE_TABLE)
                if int(item.get("id", -1)) in evidence_ids
            ]

            new_id = _next_id(CONCLUSION_TABLE)
            conclusion = {
                "id": new_id,
                "结论编号": f"RC-{new_id:04d}",
                "事件编号": event_no,
                "来源组": group,
                "根因": root_cause,
                "归因依据": _text(basis) or "；".join(text for text in evidence_texts if text),
                "证据编号": evidence_ids,
                "证据摘要": evidence_texts,
                "提交人": _text(submitter) or DEFAULT_ASSIGNEES.get(group, group),
                "提交时间": _now_text(),
                "版本": version,
                "状态": CONFIRMED,
                "有效": True,
                "关联根因": [],
                "关联确认时间": "",
                "更正前结论编号": previous_no,
                "更正前根因": previous_root,
                "更正前归因依据": previous_basis,
            }
            if related_root_cause:
                conclusion["关联根因"] = [{"来源组": "", "根因": _text(related_root_cause), "关系说明": "提交时登记"}]
            store.rows(CONCLUSION_TABLE).append(conclusion)

            if active is not None:
                active["有效"] = False
                active["状态"] = "已更正"
                active["更正时间"] = conclusion["提交时间"]
                active["被更正为"] = conclusion["结论编号"]
                # 更正后根因变化时，别组挂着的旧根因不能继续冒充“已关联”，
                # 清空关联，等两组重新确认。
                for other_group, other in self._active_conclusions(event_no).items():
                    if other_group == group:
                        continue
                    links = other.get("关联根因") or []
                    kept = [
                        link for link in links
                        if _text(link.get("来源组")) != group or _text(link.get("根因")) == root_cause
                    ]
                    if len(kept) != len(links):
                        other["关联根因"] = kept
                        other["关联确认时间"] = ""

            task = self._task_for(event_no, group, day_text) or self._latest_task(event_no, group)
            if task is not None:
                task.update({
                    "状态": CONFIRMED,
                    "结论编号": conclusion["结论编号"],
                    "根因": root_cause,
                    "证据编号": evidence_ids,
                    "更新时间": conclusion["提交时间"],
                })

        return {
            "ok": True,
            "计入": True,
            "重复提交": False,
            "结论编号": conclusion["结论编号"],
            "事件编号": event_no,
            "来源组": group,
            "根因": root_cause,
            "归因依据": conclusion["归因依据"],
            "证据编号": evidence_ids,
            "版本": version,
            "状态": CONFIRMED,
            "是否更正": active is not None,
            "更正前结论编号": previous_no,
            "更正前根因": previous_root,
            "更正前归因依据": previous_basis,
            "事件累计结论数": self._count_event_conclusions(),
            "结论记录数": len(store.rows(CONCLUSION_TABLE)),
        }

    def _latest_task(self, event_no: str, group: str) -> dict[str, Any] | None:
        matched = [
            row for row in store.rows(TASK_TABLE)
            if _text(row.get("事件编号")) == event_no and _text(row.get("来源组")) == group
        ]
        return matched[-1] if matched else None

    def _order_groups(self, conclusions: dict[str, dict[str, Any]]) -> list[str]:
        """按证据组的固定顺序输出，避免字典顺序让报表列序漂移。"""
        order = self._groups()
        return sorted(conclusions, key=lambda group: (order.index(group) if group in order else len(order), group))

    def _count_event_conclusions(self) -> int:
        """有效结论覆盖的事件数：用于核对“单事件计数”。"""
        return len({_text(row.get("事件编号")) for row in store.rows(CONCLUSION_TABLE) if row.get("有效")})

    # ------------------------------------------------------------------
    # 两组关联根因
    # ------------------------------------------------------------------
    def confirm_related_root_causes(
        self,
        incident: str | int,
        relation: str = "",
        confirmed_by: str = "",
    ) -> dict[str, Any]:
        """两组先后确认后，把彼此关联的根因互相挂到对方的结论上。

        两组必须都已有有效结论；确认顺序按结论提交时间保留，
        重复确认不会覆盖第一次的确认时间。
        """
        rows = self._event_rows(incident)
        if not rows:
            return {"ok": False, "message": f"质量事件 {incident} 不存在"}
        event_no = _event_key(rows[0])
        active = self._active_conclusions(event_no)
        if len(active) < 2:
            missing = [group for group in self._groups() if group not in active]
            return {
                "ok": False,
                "事件编号": event_no,
                "message": f"仍有证据组未确认根因：{'、'.join(missing) or '未知'}",
                "未确认组": missing,
            }

        with _SUBMIT_LOCK:
            ordered = sorted(active.items(), key=lambda item: _text(item[1].get("提交时间")))
            stamp = _now_text()
            pairs = []
            for index, (group, conclusion) in enumerate(ordered):
                others = [
                    {
                        "来源组": other_group,
                        "根因": _text(other.get("根因")),
                        "关系说明": relation or "共同导致同一质量异常",
                    }
                    for other_group, other in ordered if other_group != group
                ]
                conclusion["关联根因"] = others
                if not conclusion.get("关联确认时间"):
                    conclusion["关联确认时间"] = stamp
                conclusion["关联确认人"] = confirmed_by or _text(conclusion.get("提交人"))
                if index == 0:
                    pairs.append({
                        "先确认组": group,
                        "先确认根因": _text(conclusion.get("根因")),
                        "后确认组": ordered[1][0],
                        "后确认根因": _text(ordered[1][1].get("根因")),
                        "关系说明": relation or "共同导致同一质量异常",
                    })

        return {
            "ok": True,
            "事件编号": event_no,
            "确认顺序": [group for group, _ in ordered],
            "关联根因": pairs,
            "两组结论": {
                group: {
                    "结论编号": _text(conclusion.get("结论编号")),
                    "根因": _text(conclusion.get("根因")),
                    "归因依据": _text(conclusion.get("归因依据")),
                    "关联根因": copy.deepcopy(conclusion.get("关联根因", [])),
                    "确认时间": _text(conclusion.get("关联确认时间")),
                }
                for group, conclusion in ordered
            },
            "关联确认时间": stamp,
        }

    # ------------------------------------------------------------------
    # 质量报告修复清单
    # ------------------------------------------------------------------
    def build_remediation_list(
        self,
        report_date: str | date | None = None,
    ) -> dict[str, Any]:
        """由调查结论生成修复清单；根因待确认的事件不进入修复项。

        同一事件两个证据组指向同一根因时合并为一条修复项，
        负责组保留双方，避免质量报告把同一修复动作重复列两遍。
        """
        day_text = _parse_day(report_date).isoformat()
        items: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        seen: dict[tuple[str, str], dict[str, Any]] = {}

        for event in self._scoped_events():
            event_no = _event_key(event)
            active = self._active_conclusions(event_no)
            if not active:
                blocked.append({
                    "事件编号": event_no,
                    "关联规则": _text(event.get("关联规则")),
                    "原因": "根因待确认，暂不列入修复清单",
                })
                continue
            for group in self._order_groups(active):
                conclusion = active[group]
                cause = _text(conclusion.get("根因"))
                key = (event_no, cause)
                task = self._latest_task(event_no, group)
                fix = _REMEDIATION_BY_CAUSE.get(cause, f"按「{cause}」根因排查并修复对应链路")
                if key in seen:
                    owner = seen[key]["负责组"]
                    if group not in owner:
                        owner.append(group)
                    seen[key]["依据结论"].append(_text(conclusion.get("结论编号")))
                    continue
                item = {
                    "事件编号": event_no,
                    "关联规则": _text(event.get("关联规则")),
                    "根因": cause,
                    "修复项": fix,
                    "负责组": [group],
                    "接手人": _text((task or {}).get("接手人")),
                    "依据结论": [_text(conclusion.get("结论编号"))],
                    "归因依据": _text(conclusion.get("归因依据")),
                    "关联根因": [
                        f"{link.get('来源组')}「{link.get('根因')}」"
                        for link in conclusion.get("关联根因", [])
                    ],
                    "状态": "待修复",
                }
                seen[key] = item
                items.append(item)

        return {
            "报告日期": day_text,
            "修复清单": items,
            "修复项数": len(items),
            "未纳入修复": blocked,
            "未纳入数": len(blocked),
            "说明": "仅已确认调查结论的事件进入修复清单；根因待确认项单独核算。",
        }

    # ------------------------------------------------------------------
    # 汇总入口
    # ------------------------------------------------------------------
    def aggregate_incident_metrics(self, report_date: str | date | None = None) -> dict[str, Any]:
        """对外口径汇总：事件总量与根因待确认项分开核算。"""
        matrix = self.build_daily_root_cause_matrix(report_date)
        return {
            "报告日期": matrix["报告日期"],
            "事件总量": matrix["事件总量"],
            "事件记录数": len(store.rows("dq_incident")),
            "根因待确认项": matrix["根因待确认项"],
            "根因待确认事件数": matrix["根因待确认事件数"],
            "已确认根因项": matrix["已确认根因项"],
            "有效结论事件数": self._count_event_conclusions(),
            "结论记录数": len(store.rows(CONCLUSION_TABLE)),
            "核算口径": matrix["核算口径"],
        }

    def aggregate_quality_incident_data(self, report_date: str | date | None = None) -> dict[str, Any]:
        """质量事件数据聚合总入口：矩阵 + 口径 + 派工任务 + 修复清单。"""
        matrix = self.build_daily_root_cause_matrix(report_date)
        day_text = matrix["报告日期"]
        metrics = {
            "报告日期": day_text,
            "事件总量": matrix["事件总量"],
            "事件记录数": len(store.rows("dq_incident")),
            "根因待确认项": matrix["根因待确认项"],
            "根因待确认事件数": matrix["根因待确认事件数"],
            "已确认根因项": matrix["已确认根因项"],
            "有效结论事件数": self._count_event_conclusions(),
            "结论记录数": len(store.rows(CONCLUSION_TABLE)),
            "核算口径": matrix["核算口径"],
        }
        remediation = self.build_remediation_list(day_text)
        return {
            **metrics,
            "每日根因矩阵": matrix["矩阵"],
            "分组待查": matrix["分组待查"],
            "修复清单": remediation["修复清单"],
            "未纳入修复": remediation["未纳入修复"],
            "已派工任务": self.list_dispatched_tasks(report_date=day_text)["调查任务"],
            "生成时间": _now_text(),
        }

    # 兼容更短的调用名
    aggregate_incident_data = aggregate_quality_incident_data

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "聚合"
        snapshot["desc"] = "聚合事件证据与调查结论，生成每日根因矩阵和派工日报"
        snapshot["artifact"] = "每日根因矩阵数据集"
        snapshot["artifact_desc"] = "按事件去重并分离根因待确认口径的每日根因矩阵"
        return snapshot
