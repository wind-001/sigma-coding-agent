import { useMemo } from 'react'

/** 按当前小时返回问候语(5-11 早上 / 11-14 中午 / 14-18 下午 / 18-23 晚上 / 其余深夜) */
function getGreetingByHour(hour: number): string {
  if (hour >= 5 && hour < 11) {
    return '早上好呀，新的一天加油'
  }
  if (hour >= 11 && hour < 14) {
    return '中午好呀，去吃午饭吧'
  }
  if (hour >= 14 && hour < 18) {
    return '下午好呀，喝杯茶休息下'
  }
  if (hour >= 18 && hour < 23) {
    return '晚上好呀，今天辛苦啦'
  }
  return '夜深了，注意休息'
}

/**
 * 中央水印与问候语。水印 = "lkx"(星辰要求的字母标),居中、淡灰、不可选中。
 */
export default function Hero(): JSX.Element {
  const greeting: string = useMemo<string>(() => getGreetingByHour(new Date().getHours()), [])

  return (
    <div className="hero">
      <div className="hero__watermark hero__watermark--lkx" aria-hidden="true">
        lkx
      </div>
      <h1 className="hero__greeting">{greeting}</h1>
    </div>
  )
}
