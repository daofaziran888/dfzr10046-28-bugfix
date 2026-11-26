<template>
  <section class="page" data-module="dim_snapshot_board">
    <header class="page-head">
      <div>
        <h2>维度快照操作台</h2>
        <p class="page-desc">
          候选清单只接收拉链加工的合法闭链结果；已作废/冲突记录被剔除并注明原因。
          在途开单遇来源作废会自动退回，操作者据此判断还能否确认、需要回到哪笔拉链重选。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-board" type="button" @click="reload">刷新操作台</button>
      </div>
    </header>

    <p v-if="message" class="board-message" :class="messageOk ? 'ok-text' : 'error-text'" data-testid="board-message">
      {{ message }}
    </p>

    <article v-for="board in boards" :key="String(board.snapshot.id)" class="board-card" data-testid="snapshot-board">
      <header class="board-head">
        <strong>{{ board.snapshot['快照编号'] }}</strong>
        <span>所属维度：{{ board.snapshot['所属维度'] }}</span>
        <span>快照日期：{{ board.snapshot['快照日期'] }}</span>
        <span>状态：{{ board.snapshot.status }}</span>
      </header>

      <h4>可建档候选（合法拉链结果）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>拉链编号</th>
            <th>代理键</th>
            <th>来源状态</th>
            <th>可用区间</th>
            <th>是否当前</th>
            <th>开单资格</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="candidate in board.candidates" :key="candidate.chain_id" :data-testid="`candidate-${candidate.chain_id}`">
            <td>{{ candidate.chain_id }}</td>
            <td>{{ candidate.surrogate_key }}</td>
            <td>{{ candidate.status }}</td>
            <td>{{ candidate.interval }}</td>
            <td>{{ candidate.is_current }}</td>
            <td>{{ candidate.orderable ? '可开单' : '加工中，暂不可开单' }}</td>
            <td>
              <button
                v-if="candidate.orderable && !candidate.active_order"
                class="link"
                type="button"
                :data-testid="`accept-${candidate.chain_id}`"
                @click="acceptOrder(board.snapshot, candidate.chain_id)"
              >
                受理开单
              </button>
              <span v-else-if="candidate.active_order" class="muted-text">
                在途开单 {{ candidate.active_order }}
              </span>
            </td>
          </tr>
          <tr v-if="!board.candidates.length">
            <td colspan="7" class="empty-state">该维度暂无合法候选拉链</td>
          </tr>
        </tbody>
      </table>

      <h4>已剔除记录（不得据其开单）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>拉链编号</th>
            <th>来源状态</th>
            <th>区间</th>
            <th>剔除原因</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in board.excluded"
            :key="item.chain_id"
            class="excluded-row"
            :data-testid="`excluded-${item.chain_id}`"
          >
            <td>{{ item.chain_id }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.interval }}</td>
            <td>{{ item.reason }}</td>
          </tr>
          <tr v-if="!board.excluded.length">
            <td colspan="4" class="empty-state">无剔除记录</td>
          </tr>
        </tbody>
      </table>

      <h4>在途开单</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>开单编号</th>
            <th>来源拉链</th>
            <th>来源当前状态</th>
            <th>开单状态</th>
            <th>退回原因</th>
            <th>处理指引</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="order in board.orders" :key="order.order_id" :data-testid="`order-${order.order_id}`">
            <td>{{ order.order_id }}</td>
            <td>{{ order.chain_id }}</td>
            <td>{{ order.source_status }}</td>
            <td>{{ order.status }}</td>
            <td>{{ order.return_reason || '—' }}</td>
            <td>
              <template v-if="order.status === '已受理' && order.can_confirm">可继续确认建档</template>
              <template v-else-if="order.status === '已退回'">
                请回到拉链 {{ order.return_to_chain }} 处理拒绝后重选
              </template>
              <template v-else-if="order.status === '已确认'">已完成建档</template>
            </td>
            <td>
              <button
                v-if="order.status === '已受理'"
                class="link"
                type="button"
                :data-testid="`confirm-${order.order_id}`"
                @click="confirmOrder(order.order_id)"
              >
                确认开单
              </button>
              <button
                v-if="order.status === '已受理'"
                class="link danger"
                type="button"
                :data-testid="`reject-${order.order_id}`"
                @click="rejectOrder(order.order_id)"
              >
                退回重选
              </button>
            </td>
          </tr>
          <tr v-if="!board.orders.length">
            <td colspan="7" class="empty-state">暂无开单</td>
          </tr>
        </tbody>
      </table>
    </article>

    <footer class="page-foot">
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  acceptSnapshotOrder,
  confirmSnapshotOrder,
  loadSnapshotBoard,
  rejectSnapshotOrder,
  type SnapshotBoard,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const boards = ref<SnapshotBoard[]>([])
const errorMessage = ref('')
const message = ref('')
const messageOk = ref(true)

function notify(ok: boolean, text: string) {
  messageOk.value = ok
  message.value = text
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await loadSnapshotBoard()
    boards.value = payload.boards
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '快照操作台读取失败'
  }
}

async function acceptOrder(snapshot: EntryRow, chainId: string) {
  try {
    const result = await acceptSnapshotOrder(String(snapshot['快照编号']), chainId)
    notify(result.ok, result.message)
    if (!result.ok) return
    await reload()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '开单受理失败')
  }
}

async function confirmOrder(orderId: string) {
  try {
    const result = await confirmSnapshotOrder(orderId)
    notify(result.ok, result.message)
    await reload()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '开单确认失败')
  }
}

async function rejectOrder(orderId: string) {
  try {
    const result = await rejectSnapshotOrder(orderId)
    notify(result.ok, result.message)
    await reload()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '开单退回失败')
  }
}

onMounted(reload)
</script>

<style scoped>
.board-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 18px;
}
.board-head {
  display: flex;
  gap: 16px;
  align-items: center;
  margin-bottom: 10px;
  font-size: 13px;
}
.board-card h4 {
  margin: 14px 0 6px;
  font-size: 13px;
}
.muted-text {
  color: var(--muted);
  font-size: 12px;
}
.link.danger {
  color: #b42318;
  margin-left: 8px;
}
.excluded-row {
  background: #fef3f2;
}
.board-message {
  font-size: 13px;
}
.ok-text {
  color: #027a48;
}
</style>
