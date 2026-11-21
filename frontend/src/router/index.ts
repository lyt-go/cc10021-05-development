import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import IncidentAggregationView from '@/views/IncidentAggregationView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 聚合页是多路由页，必须排在通用 /:moduleKey 之前。
    { path: '/dq_incident_agg', name: 'incidentAgg', component: IncidentAggregationView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
