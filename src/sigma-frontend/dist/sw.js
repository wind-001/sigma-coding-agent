/* sigma 工作台通知 Service Worker(2026-10-04,星辰"点通知要自动回页面")。
 *
 * 只负责两件事:
 * 1. notificationclick —— 把**已打开的工作台页面**拉回前台(clients.focus
 *    能做到页面侧 window.focus 做不到的事:最小化/后台窗口也能 reliably
 *    聚焦);一个同源页面都没有才开新页。
 * 2. 生命周期 —— install 即 skipWaiting,activate 即 clients.claim,
 *    部署后第一个加载的页面立刻受管。
 *
 * 刻意**不写 fetch 处理器**——不拦截、不缓存任何请求,页面资源的缓存
 * 策略仍由 HTTP 头决定(工作台白屏教训:陈旧资源引用会炸整树)。
 */

self.addEventListener("install", () => {
  self.skipWaiting()
})

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener("notificationclick", (event) => {
  event.notification.close()
  event.waitUntil(
    (async () => {
      const origin = self.location.origin
      const all = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      })
      for (const client of all) {
        try {
          if (new URL(client.url).origin === origin) {
            await client.focus()
            return
          }
        } catch (e) {
          // 无法解析的 client:跳过,继续找下一个
        }
      }
      await self.clients.openWindow("/")
    })(),
  )
})
