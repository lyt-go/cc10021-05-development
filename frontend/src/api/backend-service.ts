import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 质量事件数据聚合：每日根因矩阵、派工、证据、调查结论和修复清单
// ---------------------------------------------------------------------------
export type MatrixCell = {
  来源组: string
  待查对象: string
  接手人: string
  状态: string
  根因: string
  结论编号: string
  归因依据: string
  证据条数: number
  证据摘要: string[]
  派工单号: string
  派工状态: string
  关联根因: { 来源组: string; 根因: string; 关系说明: string }[]
}

export type MatrixRow = {
  事件编号: string
  关联规则: string
  严重级别: string
  处理状态: string
  触发时间: string
  分组: Record<string, MatrixCell>
  关联根因: string[]
  尚无证据组: boolean
}

export type DailyMatrix = {
  报告日期: string
  统计范围: string
  事件总量: number
  根因待确认项: number
  已确认根因项: number
  根因待确认事件数: number
  尚无证据组事件: string[]
  分组待查: Record<string, string[]>
  矩阵: MatrixRow[]
  核算口径: string
  生成时间: string
}

export type InvestigationTask = {
  派工单号: string
  报告日期: string
  来源组: string
  事件编号: string
  待查对象: string
  接手人: string
  状态: string
  根因: string
  结论编号: string
  调查证据: { 证据编号: string; 证据类型: string; 证据摘要: string; 取得人: string; 取得时间: string }[]
  证据条数: number
  证据来源说明: string
}

export type DispatchResult = {
  报告日期: string
  事件总量: number
  根因待确认项: number
  已确认根因项: number
  尚无证据组事件: string[]
  新增派工: InvestigationTask[]
  按组派工: Record<string, number>
  单事件计数: { 事件编号: string; 事件计数: number; 派工单数: number; 派工组: string[] }[]
  报告编号: string
  核算口径: string
}

export type RemediationItem = {
  事件编号: string
  关联规则: string
  根因: string
  修复项: string
  负责组: string[]
  接手人: string
  依据结论: string[]
  归因依据: string
  关联根因: string[]
  状态: string
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    const message = detail && typeof detail.detail === 'string' ? detail.detail : fallback
    throw new Error(message)
  }
  return (await response.json()) as T
}

export async function loadIncidentMatrix(reportDate: string): Promise<DailyMatrix> {
  const query = new URLSearchParams({ report_date: reportDate }).toString()
  return readJson(await request(`/api/dq_incident_agg/matrix?${query}`), '每日根因矩阵读取失败')
}

export async function dispatchIncidentReport(
  reportDate: string,
  assignees: Record<string, string> = {},
): Promise<DispatchResult> {
  return readJson(
    await request('/api/dq_incident_agg/dispatch', {
      method: 'POST',
      body: JSON.stringify({ 报告日期: reportDate, 接手人: assignees }),
    }),
    '日报派工失败',
  )
}

export async function loadInvestigationTasks(
  reportDate: string,
  group?: string,
): Promise<{ 来源组: string; 任务数: number; 调查证据总数: number; 调查任务: InvestigationTask[] }> {
  const params = new URLSearchParams({ report_date: reportDate })
  if (group) params.set('group', group)
  return readJson(await request(`/api/dq_incident_agg/tasks?${params.toString()}`), '调查任务读取失败')
}

export async function recordIncidentEvidence(payload: {
  事件编号: string
  来源组: string
  待查对象: string
  证据摘要: string
  证据类型?: string
  取得人?: string
}): Promise<{ ok: boolean; 证据: Record<string, string> }> {
  return readJson(
    await request('/api/dq_incident_agg/evidence', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    '证据登记失败',
  )
}

export async function submitInvestigationConclusion(payload: {
  事件编号: string
  来源组: string
  根因: string
  归因依据?: string
  提交人?: string
  更正?: boolean
  报告日期?: string
}): Promise<{
  ok: boolean
  计入: boolean
  重复提交: boolean
  结论编号: string
  版本: number
  是否更正: boolean
  更正前根因: string
  更正前归因依据: string
  message?: string
}> {
  return readJson(
    await request('/api/dq_incident_agg/conclusions', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    '调查结论提交失败',
  )
}

export async function confirmRelatedRootCauses(payload: {
  事件编号: string
  关系说明?: string
  确认人?: string
}): Promise<{ ok: boolean; 确认顺序: string[]; 关联根因: unknown[]; 两组结论: Record<string, unknown> }> {
  return readJson(
    await request('/api/dq_incident_agg/related-root-causes', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    '关联根因确认失败',
  )
}

export async function loadRemediationList(
  reportDate: string,
): Promise<{ 修复清单: RemediationItem[]; 修复项数: number; 未纳入修复: { 事件编号: string; 关联规则: string; 原因: string }[] }> {
  const query = new URLSearchParams({ report_date: reportDate }).toString()
  return readJson(await request(`/api/dq_incident_agg/remediation?${query}`), '修复清单读取失败')
}
