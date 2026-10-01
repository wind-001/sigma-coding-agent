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
 * 中央水印与问候语。
 * 水印为描边 "7" 形标志,按参考图逐段测量绘制(viewBox 即参考图像素坐标):
 * 顶栏左块右缘为外凸弧线,腿部两条平行斜边向下渐隐消失;
 * 描边色 #bdbdbd 起,随 y 线性淡出。
 */
export default function Hero(): JSX.Element {
  const greeting: string = useMemo<string>(() => getGreetingByHour(new Date().getHours()), [])

  return (
    <div className="hero">
      <svg
        className="hero__watermark"
        viewBox="612 117 484 212"
        fill="none"
        aria-hidden="true"
      >
        <defs>
          <linearGradient
            id="hero-watermark-fade"
            gradientUnits="userSpaceOnUse"
            x1="0"
            y1="125"
            x2="0"
            y2="310"
          >
            <stop offset="0" stopColor="#bdbdbd" />
            <stop offset="0.55" stopColor="#c4c4c4" stopOpacity="0.85" />
            <stop offset="0.84" stopColor="#cccccc" stopOpacity="0.28" />
            <stop offset="1" stopColor="#cccccc" stopOpacity="0" />
          </linearGradient>
        </defs>
        <g
          stroke="url(#hero-watermark-fade)"
          strokeWidth="1.4"
          strokeLinejoin="round"
        >
          <path d="M662 125 L848 125 C844 152 812 170 772 180 L622 180 Z" />
          <path d="M900 125 L1084 125 L939 316 L755 316 Z" />
        </g>
      </svg>
      <h1 className="hero__greeting">{greeting}</h1>
    </div>
  )
}
