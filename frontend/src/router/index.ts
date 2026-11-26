import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SnapshotView from '@/views/SnapshotView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/dim_snapshot/board', name: 'snapshot-board', component: SnapshotView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
