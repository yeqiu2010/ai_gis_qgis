import { ref } from 'vue'
import type { AgentEvent, RPCMethod, RPCRequest, RPCResponse } from '../types/protocol'

type EventHandler = (event: AgentEvent) => void

interface QWebChannelBridge {
  sendMessage(message: string): void
  responseReady?: { connect(callback: (message: string) => void): void }
  eventEmitted?: { connect(callback: (message: string) => void): void }
}

interface QWebChannelTransport {}

interface QWebChannelInstance {
  objects: {
    bridge?: QWebChannelBridge
  }
}

declare global {
  interface Window {
    bridge?: QWebChannelBridge
    qt?: {
      webChannelTransport?: QWebChannelTransport
    }
    QWebChannel?: new (
      transport: QWebChannelTransport,
      callback: (channel: QWebChannelInstance) => void
    ) => void
  }
}

let requestId = 1

export function useBridge(onEvent: EventHandler) {
  const status = ref('未连接')
  let bridge: QWebChannelBridge | undefined
  const bridgeWaiters: Array<(value: QWebChannelBridge) => void> = []
  const pending = new Map<number, {
    resolve: (value: unknown) => void
    reject: (reason?: unknown) => void
  }>()

  function attach(nextBridge: QWebChannelBridge) {
    bridge = nextBridge
    window.bridge = nextBridge
    status.value = '已连接'
    while (bridgeWaiters.length > 0) {
      bridgeWaiters.shift()?.(nextBridge)
    }
    bridge.eventEmitted?.connect((message) => {
      onEvent(JSON.parse(message) as AgentEvent)
    })
    bridge.responseReady?.connect((message) => {
      const response = JSON.parse(message) as RPCResponse
      const waiting = pending.get(response.id)
      if (waiting) {
        pending.delete(response.id)
        if (response.error) {
          waiting.reject(response.error)
        } else {
          waiting.resolve(response.result)
        }
      }
      if (response.error) {
        status.value = response.error.message
      }
    })
  }

  connectBridge()

  async function request<T = unknown>(method: RPCMethod, params: Record<string, unknown> = {}) {
    const rpcRequest: RPCRequest = { id: requestId++, method, params }
    const activeBridge = bridge ?? await waitForBridge()
    return new Promise<T>((resolve, reject) => {
      pending.set(rpcRequest.id, { resolve: resolve as (value: unknown) => void, reject })
      activeBridge.sendMessage(JSON.stringify(rpcRequest))
    })
  }

  function waitForBridge() {
    if (bridge) {
      return Promise.resolve(bridge)
    }
    return new Promise<QWebChannelBridge>((resolve, reject) => {
      bridgeWaiters.push(resolve)
      window.setTimeout(() => {
        const index = bridgeWaiters.indexOf(resolve)
        if (index >= 0) {
          bridgeWaiters.splice(index, 1)
          status.value = 'QWebChannel bridge 不可用'
          reject(new Error(status.value))
        }
      }, 5000)
    })
  }

  function connectBridge(attempt = 0) {
    if (bridge) {
      return
    }
    if (window.bridge) {
      attach(window.bridge)
      return
    }
    if (window.qt?.webChannelTransport && window.QWebChannel) {
      new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
        if (channel.objects.bridge) {
          attach(channel.objects.bridge)
        }
      })
      return
    }

    status.value = '等待 QWebChannel'
    if (attempt < 50) {
      window.setTimeout(() => connectBridge(attempt + 1), 100)
    }
  }

  return { status, request }
}
