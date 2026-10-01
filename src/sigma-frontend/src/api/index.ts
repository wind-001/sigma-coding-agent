import type { SigmaApiClient } from './client'
import { HttpSigmaClient } from './httpClient'
import { MockSigmaClient } from './mockClient'

export * from './client'

const env = import.meta.env as { VITE_SIGMA_API_BASE?: string }

/**
 * 工作台默认直连本机桥接服务(workbench_server.py,只读,127.0.0.1:8301)——
 * "以 sigma 项目为依据":打开就是真实数据,而不是演示数据。
 * 显式设 VITE_SIGMA_API_BASE=mock 回演示模式;设其它地址则直连 σ-server(FastAPI,未实现)。
 */
const baseUrl: string = env.VITE_SIGMA_API_BASE ?? 'http://127.0.0.1:8301'

export const apiClient: SigmaApiClient =
  baseUrl === 'mock' ? new MockSigmaClient() : new HttpSigmaClient(baseUrl)
