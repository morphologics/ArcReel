import { useSyncExternalStore } from "react";

/** Refresh period of duration readouts. At second precision, denser ticks only add re-renders, not information. / 时长读数的刷新周期。秒级精度下更密的 tick 只增加重渲染，不增加信息。 */
const TICK_MS = 1000;

/**
 * App-wide shared "current time" clock.
 *
 * The timer is a **process-level singleton** rather than one per subscriber: dozens of running tasks can be visible at once, and starting a
 * `setInterval` for each would scatter the same second's re-renders across dozens of unaligned moments. With an external store
 * the timer starts with the first subscriber and stops when the last one unmounts; components need no cleanup of their own.
 *
 * 全应用共享的「当前时刻」时钟。
 *
 * 计时器是**进程级单例**而非每个订阅者一个：界面上同时可见几十个运行中任务，逐个起
 * `setInterval` 会让同一秒的重渲染分散在几十个互不对齐的时刻上。走 external store 后
 * 定时器随第一个订阅者启动、最后一个卸载时停止，无需各组件自行清理。
 */
const subscribers = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let now = Date.now();

function subscribe(onStoreChange: () => void): () => void {
  subscribers.add(onStoreChange);
  if (timer === null) {
    // While all subscribers are unmounted the clock doesn't advance; catch up first on resubscribe, otherwise until the first tick arrives
    // the readout stays at the moment the clock last stopped.
    // 全部订阅者卸载期间时钟不推进，重新订阅时先补齐，否则首个 tick 到来前
    // 读数停在上次停表的时刻。
    now = Date.now();
    timer = setInterval(() => {
      now = Date.now();
      for (const notify of subscribers) notify();
    }, TICK_MS);
  }
  return () => {
    subscribers.delete(onStoreChange);
    if (subscribers.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function getSnapshot(): number {
  return now;
}

/**
 * The current time (milliseconds), advancing once per second. Subscribe only for readouts that must move with time on their own — frozen
 * values are computed directly from a constant time; subscribing them would only cause a pointless re-render every second.
 *
 * 每秒推进一次的当前时刻（毫秒）。只在需要随时间自行走动的读数上订阅——已定格的
 * 数值直接用常量时刻计算，订阅它们只会白白每秒重渲染一次。
 */
export function useNowTick(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
