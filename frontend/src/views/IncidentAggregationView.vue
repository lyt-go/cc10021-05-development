<template>
  <section class="page" data-module="dq_incident_agg">
    <header class="page-head">
      <div>
        <h2>质量事件数据聚合</h2>
        <p class="page-desc">
          以每日根因矩阵为派工依据：事件按编号只计一次，根因待确认项单独核算，
          调查结论驱动修复清单。
        </p>
      </div>
      <div class="page-actions">
        <label class="agg-date">
          <span>报告日期</span>
          <input v-model="reportDate" type="date" data-testid="report-date" />
        </label>
        <button class="btn" data-testid="refresh-matrix" type="button" @click="reload">
          刷新矩阵
        </button>
        <button class="btn primary" data-testid="dispatch" type="button" :disabled="working" @click="dispatch">
          生成日报并派工
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text" data-testid="notice">{{ notice }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">事件总量（按事件编号去重）</span>
        <strong class="stat-value" data-testid="stat-events">{{ matrix?.事件总量 ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">根因待确认项（事件×证据组）</span>
        <strong class="stat-value" data-testid="stat-pending">{{ matrix?.根因待确认项 ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已确认根因项</span>
        <strong class="stat-value" data-testid="stat-confirmed">{{ matrix?.已确认根因项 ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">本次派工单</span>
        <strong class="stat-value" data-testid="stat-tasks">{{ dispatchResult?.新增派工.length ?? 0 }}</strong>
      </article>
    </div>

    <p class="status-legend" data-testid="caliber">{{ matrix?.核算口径 }}</p>

    <h3 class="agg-section">每日根因矩阵 · 各组待查对象</h3>
    <table class="data-table" data-testid="matrix-table">
      <thead>
        <tr>
          <th>事件编号</th>
          <th v-for="group in groupOptions" :key="group">{{ group }}待查对象 / 接手人</th>
          <th>关联根因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in matrix?.矩阵 ?? []" :key="row.事件编号" :data-testid="`matrix-row-${row.事件编号}`">
          <td>
            <strong>{{ row.事件编号 }}</strong>
            <div class="cell-sub">{{ row.关联规则 }}</div>
          </td>
          <td v-for="group in groupOptions" :key="group">
            <template v-if="row.分组[group]">
              <div>{{ row.分组[group].待查对象 }}</div>
              <div class="cell-sub">
                接手人：{{ row.分组[group].接手人 || '未指派' }} ·
                <span :data-testid="`cell-status-${row.事件编号}-${group}`">{{ row.分组[group].状态 }}</span>
                · 证据 {{ row.分组[group].证据条数 }} 条
              </div>
              <div v-if="row.分组[group].根因" class="cell-sub">
                根因：{{ row.分组[group].根因 }}（{{ row.分组[group].结论编号 }}）
              </div>
              <div class="cell-sub">{{ row.分组[group].派工单号 || '未派工' }}</div>
            </template>
            <span v-else class="cell-sub">—</span>
          </td>
          <td>
            <span v-if="!row.关联根因.length" class="cell-sub">—</span>
            <div v-for="link in row.关联根因" :key="link" class="cell-sub">{{ link }}</div>
          </td>
        </tr>
        <tr v-if="!(matrix?.矩阵 ?? []).length">
          <td :colspan="groupOptions.length + 2" class="empty-state">暂无未关闭事件</td>
        </tr>
      </tbody>
    </table>
    <p v-if="matrix?.尚无证据组事件.length" class="status-legend" data-testid="no-evidence">
      尚无证据组、待登记证据的事件：{{ matrix?.尚无证据组事件.join('、') }}
    </p>

    <h3 class="agg-section">调查结论与证据</h3>
    <form class="analysis-form" @submit.prevent="saveEvidence">
      <label>
        <span>事件编号</span>
        <input v-model="evidenceForm.事件编号" data-testid="evidence-incident" required />
      </label>
      <label>
        <span>证据组</span>
        <select v-model="evidenceForm.来源组" data-testid="evidence-group">
          <option v-for="group in groups" :key="group" :value="group">{{ group }}</option>
        </select>
      </label>
      <label>
        <span>待查对象</span>
        <input v-model="evidenceForm.待查对象" data-testid="evidence-target" required />
      </label>
      <label>
        <span>证据摘要</span>
        <input v-model="evidenceForm.证据摘要" data-testid="evidence-summary" required />
      </label>
      <label>
        <span>取得人</span>
        <input v-model="evidenceForm.取得人" data-testid="evidence-owner" />
      </label>
      <button class="btn" data-testid="save-evidence" type="submit">登记证据</button>
    </form>

    <form class="analysis-form" @submit.prevent="saveConclusion">
      <label>
        <span>事件编号</span>
        <input v-model="conclusionForm.事件编号" data-testid="conclusion-incident" required />
      </label>
      <label>
        <span>证据组</span>
        <select v-model="conclusionForm.来源组" data-testid="conclusion-group">
          <option v-for="group in groups" :key="group" :value="group">{{ group }}</option>
        </select>
      </label>
      <label>
        <span>根因</span>
        <input v-model="conclusionForm.根因" data-testid="conclusion-cause" required />
      </label>
      <label>
        <span>归因依据</span>
        <input v-model="conclusionForm.归因依据" data-testid="conclusion-basis" />
      </label>
      <label class="agg-check">
        <span>更正已有结论</span>
        <input v-model="conclusionForm.更正" type="checkbox" data-testid="conclusion-correction" />
      </label>
      <button class="btn primary" data-testid="save-conclusion" type="submit">提交结论</button>
      <button class="btn" data-testid="confirm-related" type="button" @click="confirmRelated">
        确认两组关联根因
      </button>
    </form>

    <div v-if="lastConclusion" class="analysis-card" data-testid="conclusion-result">
      <div>结论 {{ lastConclusion.结论编号 }} · 版本 {{ lastConclusion.版本 }}</div>
      <div class="cell-sub">
        {{ lastConclusion.计入 ? '已计入' : '未重复计入' }}
        <template v-if="lastConclusion.重复提交">（该事件该组已有有效结论）</template>
        <template v-if="lastConclusion.是否更正">
          ；更正前根因：{{ lastConclusion.更正前根因 }}，依据：{{ lastConclusion.更正前归因依据 || '—' }}
        </template>
      </div>
      <div v-if="lastConclusion.message" class="cell-sub">{{ lastConclusion.message }}</div>
    </div>

    <h3 class="agg-section">调查任务 · 接手人实际取得的证据</h3>
    <div class="page-actions agg-task-actions">
      <label v-for="group in groups" :key="group" class="agg-date">
        <span>{{ group }}参与者</span>
        <input v-model="assignees[group]" :data-testid="`assignee-${group}`" />
      </label>
      <label class="agg-date">
        <span>查看证据组</span>
        <select v-model="taskGroup" data-testid="task-group" @change="loadTasks">
          <option v-for="group in groups" :key="group" :value="group">{{ group }}</option>
        </select>
      </label>
    </div>
    <table class="data-table" data-testid="task-table">
      <thead>
        <tr><th>派工单号</th><th>事件编号</th><th>待查对象</th><th>接手人</th><th>状态</th><th>实际取得的调查证据</th></tr>
      </thead>
      <tbody>
        <tr v-for="task in tasks" :key="task.派工单号" :data-testid="`task-${task.派工单号}`">
          <td>{{ task.派工单号 }}</td>
          <td>{{ task.事件编号 }}</td>
          <td>{{ task.待查对象 }}</td>
          <td>{{ task.接手人 }}</td>
          <td>{{ task.状态 }}</td>
          <td>
            <div v-for="item in task.调查证据" :key="item.证据编号" class="cell-sub">
              {{ item.证据编号 }} {{ item.证据类型 }}：{{ item.证据摘要 }}（{{ item.取得人 }}）
            </div>
            <span v-if="!task.调查证据.length" class="cell-sub">{{ task.证据来源说明 }}</span>
          </td>
        </tr>
        <tr v-if="!tasks.length">
          <td colspan="6" class="empty-state">当天还没有派工任务</td>
        </tr>
      </tbody>
    </table>

    <h3 class="agg-section">单事件计数核对</h3>
    <table class="data-table" data-testid="count-table">
      <thead>
        <tr><th>事件编号</th><th>事件计数</th><th>派工单数</th><th>派给各组</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in dispatchResult?.单事件计数 ?? []" :key="item.事件编号">
          <td>{{ item.事件编号 }}</td>
          <td>{{ item.事件计数 }}</td>
          <td>{{ item.派工单数 }}</td>
          <td>{{ item.派工组.join('、') }}</td>
        </tr>
        <tr v-if="!(dispatchResult?.单事件计数 ?? []).length">
          <td colspan="4" class="empty-state">派工后在这里核对单事件计数</td>
        </tr>
      </tbody>
    </table>

    <h3 class="agg-section">质量报告修复清单</h3>
    <table class="data-table" data-testid="remediation-table">
      <thead>
        <tr><th>事件编号</th><th>根因</th><th>修复项</th><th>负责组</th><th>接手人</th><th>归因依据</th><th>关联根因</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in remediation?.修复清单 ?? []" :key="`${item.事件编号}-${item.根因}`">
          <td>{{ item.事件编号 }}</td>
          <td>{{ item.根因 }}</td>
          <td>{{ item.修复项 }}</td>
          <td>{{ item.负责组.join('、') }}</td>
          <td>{{ item.接手人 }}</td>
          <td class="cell-sub">{{ item.归因依据 }}</td>
          <td class="cell-sub">{{ item.关联根因.join('；') || '—' }}</td>
        </tr>
        <tr v-if="!(remediation?.修复清单 ?? []).length">
          <td colspan="7" class="empty-state">根因待确认，尚无修复项</td>
        </tr>
      </tbody>
    </table>
    <p v-if="remediation?.未纳入修复.length" class="status-legend">
      未纳入修复（根因待确认）：{{ remediation?.未纳入修复.map((item) => item.事件编号).join('、') }}
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  confirmRelatedRootCauses,
  dispatchIncidentReport,
  loadIncidentMatrix,
  loadInvestigationTasks,
  loadRemediationList,
  recordIncidentEvidence,
  submitInvestigationConclusion,
} from '@/api/backend-service'
import type {
  DailyMatrix,
  DispatchResult,
  InvestigationTask,
  RemediationItem,
} from '@/api/backend-service'

const today = new Date().toISOString().slice(0, 10)
const reportDate = ref(today)
const groups = ['采集侧', '转换侧']
const matrix = ref<DailyMatrix | null>(null)
const dispatchResult = ref<DispatchResult | null>(null)
const remediation = ref<{ 修复清单: RemediationItem[]; 未纳入修复: { 事件编号: string }[] } | null>(null)
const tasks = ref<InvestigationTask[]>([])
const taskGroup = ref('采集侧')
const assignees = reactive<Record<string, string>>({})
// 矩阵列按数据里实际出现的证据组展开，避免漏掉“未分组”之类的额外列。
const groupOptions = computed(() => {
  const found = new Set(groups)
  for (const row of matrix.value?.矩阵 ?? []) {
    for (const group of Object.keys(row.分组)) found.add(group)
  }
  for (const group of Object.keys(matrix.value?.分组待查 ?? {})) found.add(group)
  return [...found]
})
const working = ref(false)
const errorMessage = ref('')
const notice = ref('')
const lastConclusion = ref<{
  计入: boolean
  重复提交: boolean
  结论编号: string
  版本: number
  是否更正: boolean
  更正前根因: string
  更正前归因依据: string
  message?: string
} | null>(null)

const evidenceForm = reactive({ 事件编号: '', 来源组: '采集侧', 待查对象: '', 证据摘要: '', 取得人: '' })
const conclusionForm = reactive({ 事件编号: '', 来源组: '采集侧', 根因: '', 归因依据: '', 更正: false })

async function reload() {
  errorMessage.value = ''
  try {
    matrix.value = await loadIncidentMatrix(reportDate.value)
    remediation.value = await loadRemediationList(reportDate.value)
    await loadTasks()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合数据读取失败'
  }
}

async function loadTasks() {
  try {
    const payload = await loadInvestigationTasks(reportDate.value, taskGroup.value)
    tasks.value = payload.调查任务
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调查任务读取失败'
  }
}

async function dispatch() {
  working.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    dispatchResult.value = await dispatchIncidentReport(reportDate.value, assignees)
    notice.value = `已生成日报 ${dispatchResult.value.报告编号}：事件总量 ${dispatchResult.value.事件总量}，根因待确认项 ${dispatchResult.value.根因待确认项}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派工失败'
  } finally {
    working.value = false
  }
}

async function saveEvidence() {
  errorMessage.value = ''
  try {
    const result = await recordIncidentEvidence(evidenceForm)
    notice.value = `已登记证据 ${result.证据.证据编号}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '证据登记失败'
  }
}

async function saveConclusion() {
  errorMessage.value = ''
  try {
    lastConclusion.value = await submitInvestigationConclusion({
      ...conclusionForm,
      报告日期: reportDate.value,
    })
    notice.value = lastConclusion.value.计入 ? '调查结论已计入' : '未重复计入'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结论提交失败'
  }
}

async function confirmRelated() {
  errorMessage.value = ''
  try {
    const result = await confirmRelatedRootCauses({ 事件编号: conclusionForm.事件编号 })
    notice.value = `两组关联根因已确认：${result.确认顺序.join(' → ')}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '关联根因确认失败'
  }
}

onMounted(reload)
</script>
