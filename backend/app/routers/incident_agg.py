"""质量事件数据聚合接口：每日根因矩阵、派工、证据、调查结论和修复清单。

聚合服务有自己的业务动作（派工、提交结论、关联根因），不适合复用
通用资源路由的 `{ok, message}` 动作约定，所以单独挂一组只读/写入接口。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.services.dq_incident_agg import DqIncidentAggService

router = APIRouter(prefix="/api/dq_incident_agg", tags=["质量事件数据聚合"])


def service() -> DqIncidentAggService:
    # 服务本身无状态，明细都在 store 里；每次请求新建即可。
    return DqIncidentAggService()


@router.get("/matrix")
def daily_matrix(report_date: str | None = Query(default=None)) -> dict[str, Any]:
    """每日根因矩阵：事件为行、证据组为列，列出各组待查对象。"""
    return service().build_daily_root_cause_matrix(report_date)


@router.get("/metrics")
def incident_metrics(report_date: str | None = Query(default=None)) -> dict[str, Any]:
    """对外口径：事件总量与根因待确认项分开核算。"""
    return service().aggregate_incident_metrics(report_date)


@router.get("/aggregate")
def aggregate(report_date: str | None = Query(default=None)) -> dict[str, Any]:
    """质量事件数据聚合总览：矩阵 + 口径 + 任务 + 修复清单。"""
    return service().aggregate_quality_incident_data(report_date)


@router.post("/evidence")
def record_evidence(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    result = service().record_investigation_evidence(
        payload.get("事件编号") or payload.get("incident") or "",
        payload.get("来源组") or payload.get("group") or "",
        payload.get("待查对象") or payload.get("target") or "",
        payload.get("证据摘要") or payload.get("summary") or "",
        evidence_type=payload.get("证据类型") or payload.get("evidence_type") or "",
        obtained_by=payload.get("取得人") or payload.get("obtained_by") or "",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "证据登记失败"))
    return result


@router.post("/dispatch")
def dispatch(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """按每日根因矩阵派工，重复执行幂等。"""
    assignees = payload.get("接手人") or payload.get("assignees") or {}
    if not isinstance(assignees, dict):
        raise HTTPException(status_code=422, detail="接手人必须是 {证据组: 姓名} 结构")
    return service().dispatch_daily_investigations(
        payload.get("报告日期") or payload.get("report_date"),
        {str(key): str(value) for key, value in assignees.items()},
    )


@router.get("/tasks")
def list_tasks(
    group: str | None = Query(default=None),
    report_date: str | None = Query(default=None),
    incident: str | None = Query(default=None),
    assignee: str | None = Query(default=None),
) -> dict[str, Any]:
    """分别查看各组发出的调查任务和接手人实际取得的证据。"""
    return service().list_dispatched_tasks(
        group=group, report_date=report_date, incident=incident, assignee=assignee)


@router.post("/conclusions")
def submit_conclusion(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """提交调查结论；并发重复提交只计入一次，更正保留当时依据。"""
    result = service().submit_investigation_conclusion(
        payload.get("事件编号") or payload.get("incident") or "",
        payload.get("来源组") or payload.get("group") or "",
        payload.get("根因") or payload.get("root_cause") or "",
        basis=payload.get("归因依据") or payload.get("basis") or "",
        evidence_ids=payload.get("证据编号") or payload.get("evidence_ids"),
        submitter=payload.get("提交人") or payload.get("submitter") or "",
        correction=bool(payload.get("更正") or payload.get("correction")),
        related_root_cause=payload.get("关联根因") or payload.get("related_root_cause") or "",
        report_date=payload.get("报告日期") or payload.get("report_date"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "调查结论提交失败"))
    return result


@router.post("/related-root-causes")
def confirm_related(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """两组先后确认后，互相挂上对方的根因。"""
    result = service().confirm_related_root_causes(
        payload.get("事件编号") or payload.get("incident") or "",
        relation=payload.get("关系说明") or payload.get("relation") or "",
        confirmed_by=payload.get("确认人") or payload.get("confirmed_by") or "",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail={
            "message": result.get("message"),
            "未确认组": result.get("未确认组", []),
        })
    return result


@router.get("/remediation")
def remediation(report_date: str | None = Query(default=None)) -> dict[str, Any]:
    """调查结论驱动的修复清单。"""
    return service().build_remediation_list(report_date)
