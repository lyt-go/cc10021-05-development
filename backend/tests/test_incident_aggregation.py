"""业务测试：覆盖质量事件数据聚合的根因矩阵、派工、结论去重和修复清单。"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.dq_incident_agg import (
    CONCLUSION_TABLE,
    EVIDENCE_TABLE,
    TASK_TABLE,
    DqIncidentAggService,
)
from app.services.dq_report import DqReportService
from app.store import store

DAY = "2026-11-21"


@pytest.fixture(autouse=True)
def clean_agg_tables():
    """每个用例都从干净的聚合明细表开始，并还原事件示例数据。"""
    tables = [EVIDENCE_TABLE, TASK_TABLE, CONCLUSION_TABLE, "_incident_agg_matrix"]
    saved = {
        "dq_incident": [dict(row) for row in store.rows("dq_incident")],
        DqIncidentAggService.MODULE: [dict(row) for row in store.rows(DqIncidentAggService.MODULE)],
    }
    for table in tables:
        store.rows(table).clear()
    store.rows(DqIncidentAggService.MODULE).clear()
    # 默认事件表也清空：每个用例自己用 add_event 造数，避免示例数据混进计数。
    store.rows("dq_incident").clear()
    yield
    for table in tables:
        store.rows(table).clear()
    for table, rows in saved.items():
        store.rows(table)[:] = rows


def add_event(event_no: str, rule: str, status: str = "调查中") -> None:
    rows = store.rows("dq_incident")
    rows.append({
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "事件编号": event_no,
        "关联规则": rule,
        "触发时间": "2026-11-20",
        "严重级别": "warning",
        "处理状态": status,
        "status": status,
        "pending": True,
        "abnormal": True,
    })


def add_evidence(svc: DqIncidentAggService, event_no: str, group: str,
                 target: str, summary: str, owner: str) -> None:
    result = svc.record_investigation_evidence(event_no, group, target, summary, obtained_by=owner)
    assert result["ok"] is True


class TestIncidentAggregationCounts:
    """事件总量和根因待确认项分开核算"""

    def test_duplicate_event_number_counted_once(self):
        add_event("DQ_I-0001", "ods_order_info")
        add_event("DQ_I-0001", "ods_order_info")
        matrix = DqIncidentAggService().build_daily_root_cause_matrix(DAY)
        assert matrix["事件总量"] == 1
        assert matrix["矩阵"][0]["事件计数"] == 1

    def test_closed_events_not_in_matrix(self):
        add_event("DQ_I-0001", "ods_order_info", status="已关闭")
        add_event("DQ_I-0002", "ods_order_info")
        matrix = DqIncidentAggService().build_daily_root_cause_matrix(DAY)
        assert matrix["事件总量"] == 1
        assert matrix["矩阵"][0]["事件编号"] == "DQ_I-0002"

    def test_pending_items_do_not_inflate_event_total(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        add_evidence(svc, "DQ_I-0001", "采集侧", "上游分区", "采集到 0 行", "李采集")
        add_evidence(svc, "DQ_I-0001", "转换侧", "过滤条件", "过滤条件变更", "王转换")
        matrix = svc.build_daily_root_cause_matrix(DAY)
        assert matrix["事件总量"] == 1
        assert matrix["根因待确认项"] == 2
        assert matrix["已确认根因项"] == 0

    def test_event_without_evidence_is_pending_not_extra_event(self):
        add_event("DQ_I-0001", "ods_order_info")
        matrix = DqIncidentAggService().build_daily_root_cause_matrix(DAY)
        assert matrix["事件总量"] == 1
        assert matrix["根因待确认项"] == 1
        assert matrix["尚无证据组事件"] == ["DQ_I-0001"]

    def test_incident_summary_has_root_cause_view(self):
        from app.services.dq_incident import DqIncidentService

        add_event("DQ_I-0001", "ods_order_info F1")
        svc = DqIncidentAggService()
        add_evidence(svc, "DQ_I-0001", "采集侧", "上游分区", "采集到 0 行", "李采集")
        svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "上游分区未产出")

        summary = DqIncidentService().aggregate_incident_summary("ods_order_info")
        assert summary["事件总数"] == 1
        assert summary["根因视图"][0]["根因状态"] == "已确认"
        assert summary["根因视图"][0]["已确认根因"][0]["根因"] == "缺数"
        assert summary["根因待确认项"] == 0


class TestDispatch:
    """每日根因矩阵作为派工依据"""

    def test_dispatch_creates_one_task_per_group(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        add_evidence(svc, "DQ_I-0001", "采集侧", "上游分区", "采集到 0 行", "李采集")
        add_evidence(svc, "DQ_I-0001", "转换侧", "过滤条件", "过滤条件变更", "王转换")
        result = svc.dispatch_daily_investigations(DAY)
        assert len(result["新增派工"]) == 2
        assert result["按组派工"]["采集侧"] == 1
        assert result["按组派工"]["转换侧"] == 1
        per_event = {item["事件编号"]: item for item in result["单事件计数"]}
        assert per_event["DQ_I-0001"]["事件计数"] == 1
        assert per_event["DQ_I-0001"]["派工单数"] == 2

    def test_dispatch_is_idempotent(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        add_evidence(svc, "DQ_I-0001", "采集侧", "上游分区", "采集到 0 行", "李采集")
        svc.dispatch_daily_investigations(DAY)
        again = svc.dispatch_daily_investigations(DAY)
        assert again["新增派工"] == []
        assert len(store.rows(TASK_TABLE)) == 1

    def test_dispatch_skips_confirmed_group_only(self):
        """日报发出时仍有一组在核查：只给未确认组派工，事件仍只计一次。"""
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        add_evidence(svc, "DQ_I-0001", "采集侧", "上游分区", "采集到 0 行", "李采集")
        add_evidence(svc, "DQ_I-0001", "转换侧", "过滤条件", "过滤条件变更", "王转换")
        svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "上游分区未产出")

        result = svc.dispatch_daily_investigations(DAY)
        assert result["事件总量"] == 1
        assert result["根因待确认项"] == 1
        assert result["已确认根因项"] == 1
        assert [task["来源组"] for task in result["新增派工"]] == ["转换侧"]
        assert result["单事件计数"][0]["事件计数"] == 1
        assert result["单事件计数"][0]["派工组"] == ["转换侧"]

    def test_assignee_only_sees_own_evidence(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        add_evidence(svc, "DQ_I-0001", "采集侧", "上游分区", "采集到 0 行", "李采集")
        add_evidence(svc, "DQ_I-0001", "转换侧", "过滤条件", "过滤条件变更", "王转换")
        svc.dispatch_daily_investigations(DAY)

        collect = svc.list_dispatched_tasks("采集侧", DAY)
        transform = svc.list_dispatched_tasks("转换侧", DAY)
        assert collect["任务数"] == 1
        assert [item["证据摘要"] for item in collect["调查任务"][0]["调查证据"]] == ["采集到 0 行"]
        assert [item["证据摘要"] for item in transform["调查任务"][0]["调查证据"]] == ["过滤条件变更"]
        assert transform["调查任务"][0]["调查证据"][0]["取得人"] == "王转换"


class TestConclusionDedup:
    """并发提交只计一次，更正保留当时依据"""

    def test_duplicate_submission_not_counted_twice(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        first = svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "上游分区未产出")
        second = svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "再次提交")
        assert first["计入"] is True
        assert second["计入"] is False and second["重复提交"] is True
        assert first["结论编号"] == second["结论编号"]
        assert second["事件累计结论数"] == 1
        assert len(store.rows(CONCLUSION_TABLE)) == 1

    def test_concurrent_submissions_only_one_wins(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        results: list[dict] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def submit(index: int) -> None:
            barrier.wait()
            outcome = DqIncidentAggService().submit_investigation_conclusion(
                "DQ_I-0001", "采集侧", "缺数", f"依据{index}")
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(1 for item in results if item["计入"]) == 1
        assert len(store.rows(CONCLUSION_TABLE)) == 1

    def test_correction_keeps_previous_basis(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "当时以为是上游缺数")
        corrected = svc.submit_investigation_conclusion(
            "DQ_I-0001", "采集侧", "上游延迟", "补查调度日志", correction=True)

        assert corrected["计入"] is True
        assert corrected["版本"] == 2
        assert corrected["更正前根因"] == "缺数"
        assert corrected["更正前归因依据"] == "当时以为是上游缺数"
        assert corrected["事件累计结论数"] == 1
        assert len(store.rows(CONCLUSION_TABLE)) == 2

        old = store.rows(CONCLUSION_TABLE)[0]
        assert old["有效"] is False and old["状态"] == "已更正"

        matrix = svc.build_daily_root_cause_matrix(DAY)
        assert matrix["矩阵"][0]["分组"]["采集侧"]["根因"] == "上游延迟"
        assert matrix["已确认根因项"] == 1

    def test_same_event_two_groups_count_once(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "上游分区未产出")
        svc.submit_investigation_conclusion("DQ_I-0001", "转换侧", "过滤条件变化", "过滤条件变更")
        metrics = svc.aggregate_incident_metrics(DAY)
        assert metrics["事件总量"] == 1
        assert metrics["结论记录数"] == 2
        assert metrics["有效结论事件数"] == 1
        assert metrics["根因待确认项"] == 0


class TestRelatedRootCauses:
    """两组先后确认彼此关联的根因"""

    def test_related_requires_both_groups(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "上游分区未产出")
        result = svc.confirm_related_root_causes("DQ_I-0001")
        assert result["ok"] is False
        assert result["未确认组"] == ["转换侧"]

    def test_related_links_both_directions_in_order(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "上游分区未产出")
        svc.submit_investigation_conclusion("DQ_I-0001", "转换侧", "过滤条件变化", "过滤条件变更")
        result = svc.confirm_related_root_causes("DQ_I-0001", relation="缺数叠加过滤条件变更")

        assert result["ok"] is True
        assert result["确认顺序"] == ["采集侧", "转换侧"]
        assert result["两组结论"]["采集侧"]["关联根因"][0]["根因"] == "过滤条件变化"
        assert result["两组结论"]["转换侧"]["关联根因"][0]["根因"] == "缺数"

        matrix = svc.build_daily_root_cause_matrix(DAY)
        assert matrix["矩阵"][0]["关联根因"]
        assert matrix["已确认根因项"] == 2

    def test_correction_clears_stale_related_link(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "上游分区未产出")
        svc.submit_investigation_conclusion("DQ_I-0001", "转换侧", "过滤条件变化", "过滤条件变更")
        svc.confirm_related_root_causes("DQ_I-0001")
        svc.submit_investigation_conclusion(
            "DQ_I-0001", "采集侧", "上游延迟", "补查调度日志", correction=True)

        matrix = svc.build_daily_root_cause_matrix(DAY)
        transform_cell = matrix["矩阵"][0]["分组"]["转换侧"]
        assert transform_cell["关联根因"] == []


class TestReportIntegration:
    """调查结论驱动质量报告修复清单"""

    def test_remediation_only_uses_confirmed_conclusions(self):
        add_event("DQ_I-0001", "ods_order_info")
        add_event("DQ_I-0002", "dwd_trade_detail")
        svc = DqIncidentAggService()
        svc.submit_investigation_conclusion("DQ_I-0001", "转换侧", "过滤条件变化", "过滤条件变更")

        remediation = svc.build_remediation_list(DAY)
        assert [item["事件编号"] for item in remediation["修复清单"]] == ["DQ_I-0001"]
        assert "过滤条件" in remediation["修复清单"][0]["修复项"]
        assert [item["事件编号"] for item in remediation["未纳入修复"]] == ["DQ_I-0002"]

    def test_remediation_does_not_duplicate_same_cause_across_groups(self):
        add_event("DQ_I-0001", "ods_order_info")
        svc = DqIncidentAggService()
        svc.submit_investigation_conclusion("DQ_I-0001", "采集侧", "缺数", "上游分区未产出")
        svc.submit_investigation_conclusion("DQ_I-0001", "转换侧", "缺数", "转换侧也判定缺数")
        remediation = svc.build_remediation_list(DAY)
        assert remediation["修复项数"] == 1
        assert remediation["修复清单"][0]["负责组"] == ["采集侧", "转换侧"]
        assert len(remediation["修复清单"][0]["依据结论"]) == 2

    def test_report_includes_root_cause_and_remediation(self):
        add_event("DQ_I-0001", "ods_order_info")
        DqIncidentAggService().submit_investigation_conclusion(
            "DQ_I-0001", "采集侧", "缺数", "上游分区未产出")
        report = DqReportService().aggregate_quality_report("daily")
        assert report["质量事件总数"] == 1
        assert report["根因待确认项"] == 0
        assert report["修复清单"][0]["根因"] == "缺数"


class TestAggregationApi:
    """聚合接口能互相对账（路由函数直调，环境无 httpx 时也能跑）"""

    def test_routes_registered(self):
        from app.routers import incident_agg

        paths = {route.path for route in incident_agg.router.routes}
        assert "/api/dq_incident_agg/matrix" in paths
        assert "/api/dq_incident_agg/dispatch" in paths
        assert "/api/dq_incident_agg/conclusions" in paths
        assert "/api/dq_incident_agg/tasks" in paths
        assert "/api/dq_incident_agg/remediation" in paths

    def test_endpoints_agree_on_counts(self):
        from app.routers import incident_agg

        add_event("DQ_I-0001", "ods_order_info")
        incident_agg.record_evidence({
            "事件编号": "DQ_I-0001", "来源组": "采集侧",
            "待查对象": "上游分区", "证据摘要": "采集到 0 行", "取得人": "李采集",
        })
        dispatched = incident_agg.dispatch({"报告日期": DAY})
        assert dispatched["事件总量"] == 1
        assert dispatched["根因待确认项"] == 1

        tasks = incident_agg.list_tasks(group="采集侧", report_date=DAY, incident=None, assignee=None)
        assert tasks["任务数"] == 1
        assert tasks["调查任务"][0]["调查证据"][0]["证据摘要"] == "采集到 0 行"

        metrics = incident_agg.incident_metrics(report_date=DAY)
        assert metrics["事件总量"] == dispatched["事件总量"]
        assert metrics["根因待确认项"] == dispatched["根因待确认项"]
