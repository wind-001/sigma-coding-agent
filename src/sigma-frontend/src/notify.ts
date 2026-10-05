/**
 * 系统通知(Web Notification API,2026-10-03 星辰拍板"Web 通知 API"方案)。
 *
 * human-in-the-loop 机制下面板必须能"追着用户喊":审批待确认 / 任务完成 /
 * 任务失败这三类事件,若用户此刻**没在看面板**(标签页最小化、切到别的
 * 窗口压在上面),就发一条系统级通知(Windows 通知中心);正看着就只走
 * 面板内 UI,不打扰。
 *
 * "用户在看"的操作性定义 = document.visibilityState === 'visible'
 * **且** document.hasFocus()——前者抓"标签页整个被藏起来"(最小化/切标签),
 * 后者抓"面板还露着但焦点在别的窗口"(不在最上方)。两者都满足才算在看。
 *
 * 权限:浏览器要求一次性授权,且授权请求必须在**用户手势**里发起(部分
 * 浏览器无手势直接拒)。所以挂在"发送消息"这类必然存在的点击上,静默
 * 失败——未授权时 maybeNotify 直接跳过,面板内 UI 不受任何影响
 * (通知是增强,不是主路径)。
 */

/** 是否具备发系统通知的浏览器能力。 */
export function notificationsSupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window
}

/** 当前授权状态;'unsupported' = 浏览器没有这套 API。 */
export function notificationPermissionState(): NotificationPermission | 'unsupported' {
  if (!notificationsSupported()) return 'unsupported'
  return Notification.permission
}

/**
 * 发起授权请求。**只在用户手势处理器里调用**(无手势时部分浏览器直接拒)。
 * 已经授权/明确拒绝过就不再问——拒绝后每次都弹请求框是骚扰。
 * 静默吞掉一切异常:通知是增强功能,授权流程本身不允许打扰主路径。
 */
export function requestNotificationPermission(): void {
  if (!notificationsSupported()) return
  if (Notification.permission !== 'default') return
  ensureServiceWorker()
  try {
    const result = Notification.requestPermission()
    if (result !== undefined && typeof result.then === 'function') {
      result.then(
        () => undefined,
        () => undefined,
      )
    }
  } catch {
    // 旧实现(回调式)或被策略禁用:静默放弃,不影响面板
  }
}

// ---------------------------------------------------------------------------
// 常驻通知(星辰 2026-10-04):系统默认 ~5s 就把通知收进通知中心,审批
// 这类事很容易错过。requireInteraction = 通知**常驻到用户交互为止**;
// 配套的"回面板即收"清理:用户只要回到了工作台(窗口聚焦/页面可见),
// 没点掉的通知就全部收起——没人需要对着一条已经过时的通知再点一次。
// ---------------------------------------------------------------------------

const activeNotifications: Notification[] = []
let dismissListenersBound = false

// ---------------------------------------------------------------------------
// Service Worker 通道(星辰 2026-10-04"点通知要自动回页面"):页面侧
// window.focus() 对最小化/后台窗口常被浏览器拦掉;SW 的 notificationclick
// 里 clients.focus() 是 reliably 把已有页面拉回前台的标准做法(PWA 通知
// 回跳)。注册成功 → 通知走 SW showNotification;失败/未就绪 → 页面
// Notification 兜底(onclick 里仍试 window.focus)。sw.js 不拦 fetch,
// 只管点击回跳,不引入缓存陈旧资源的问题。
// ---------------------------------------------------------------------------

let swRegistration: ServiceWorkerRegistration | null = null
let swRegistrationStarted = false

function ensureServiceWorker(): void {
  if (swRegistrationStarted) return
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return
  swRegistrationStarted = true
  navigator.serviceWorker
    .register('/sw.js', { scope: '/' })
    .then((registration: ServiceWorkerRegistration): void => {
      swRegistration = registration
    })
    .catch((): void => {
      // 注册失败(非安全上下文/策略禁用):静默走页面 Notification 兜底
      swRegistrationStarted = false
    })
}

function dismissServiceWorkerNotifications(): void {
  const registration = swRegistration
  if (registration === null) return
  registration.getNotifications()
    .then((notifications: Notification[]): void => {
      for (const notification of notifications) notification.close()
    })
    .catch((): void => undefined)
}

function bindDismissOnReturn(): void {
  if (dismissListenersBound || typeof window === 'undefined') return
  dismissListenersBound = true
  const dismissAll = (): void => {
    for (const notification of activeNotifications.splice(0)) {
      try {
        notification.close()
      } catch {
        // 已被系统收走/关闭:无需处理
      }
    }
    dismissServiceWorkerNotifications()
  }
  window.addEventListener('focus', dismissAll)
  document.addEventListener('visibilitychange', (): void => {
    if (document.visibilityState === 'visible') dismissAll()
  })
}

/** 页面 Notification 路径(SW 不可用/超时时的兜底)。 */
function showPageNotification(title: string, body: string, options?: {
  onClick?: () => void
  tag?: string
}): boolean {
  try {
    const notification = new Notification(title, {
      body: body,
      tag: options?.tag,
      requireInteraction: true,
    })
    activeNotifications.push(notification)
    notification.onclose = (): void => {
      const index = activeNotifications.indexOf(notification)
      if (index >= 0) activeNotifications.splice(index, 1)
    }
    notification.onclick = (): void => {
      window.focus()
      options?.onClick?.()
      notification.close()
    }
    return true
  } catch {
    return false
  }
}

/**
 * 用户没在看面板时发一条系统通知;正看着就不弹。返回是否真的弹了。
 *
 * 通知**常驻**(requireInteraction),消失只有两条路:用户点了它
 * (window.focus + onClick 回面板,通知随即关闭),或用户以任何方式
 * 回到了工作台(聚焦/可见,见 bindDismissOnReturn)。
 *
 * `tag` 相同的通知会互相**替换**而不是堆叠(轮询每 500ms 一次,
 * 不去重的话同一件事会刷屏通知中心)。
 *
 * `onClick`:点击通知后的动作;`window.focus()` 无条件执行
 * (点通知 = 用户想回到面板),额外的导航由调用方传入。
 */
export function maybeNotify(title: string, body: string, options?: {
  onClick?: () => void
  tag?: string
}): boolean {
  if (document.visibilityState === 'visible' && document.hasFocus()) return false
  if (!notificationsSupported() || Notification.permission !== 'granted') return false
  ensureServiceWorker()
  bindDismissOnReturn()
  // SW 通道优先:点击回跳由 sw.js 的 notificationclick 处理(clients.focus)。
  // **1.5s 内没确认展示成功就落回页面通知**——有的环境(内嵌 webview)
  // showNotification 的 promise 会静默挂起,fire-and-forget 等于通知丢失;
  // 页面 Notification 路径在这些环境实测可用,宁可多兜一层。
  const registration = swRegistration
  if (registration !== null) {
    void (async (): Promise<void> => {
      try {
        const shown = await Promise.race([
          registration
            .showNotification(title, {
              body: body,
              tag: options?.tag,
              requireInteraction: true,
            })
            .then((): boolean => true),
          new Promise<boolean>((resolve) => {
            setTimeout(() => resolve(false), 1500)
          }),
        ])
        if (shown) return
      } catch {
        // SW 展示被拒/失败:落回页面通知
      }
      showPageNotification(title, body, options)
    })()
    return true
  }
  return showPageNotification(title, body, options)
}
