import { createContext, useCallback, useContext, useState } from "react";

type TrackWrite = <T>(work: Promise<T>) => Promise<T>;

const AssetWriteContext = createContext<TrackWrite | null>(null);

/** Write registration the detail editor provides to each section: uploads, deletions and alias adds/removes in a section are counted into the busy state through it. / 详情编辑器向各区块提供的写入登记：区块里的上传、删除、别名增删都经它计入占用态。 */
export const AssetWriteProvider = AssetWriteContext.Provider;

/**
 * Write busy state of the detail editor: `writing` is true while any section has a write request in flight, and rename, generation and other write entries
 * are disabled together. These requests are local state, not in the task queue, so `rejectIfAssetBusy` can't see them.
 *
 * 详情编辑器的写入占用：任一区块有写请求在途时 `writing` 为 true，改名、生成与其它写入入口
 * 一起禁用。这些请求都是本地 state、不进任务队列，`rejectIfAssetBusy` 看不见。
 */
export function useAssetWrites(): { writing: boolean; track: TrackWrite } {
  const [count, setCount] = useState(0);
  const track = useCallback(async <T,>(work: Promise<T>) => {
    setCount((c) => c + 1);
    try {
      return await work;
    } finally {
      setCount((c) => c - 1);
    }
  }, []);
  return { writing: count > 0, track };
}

/** Register one write request within a section; returned unchanged when not inside the detail editor. / 区块内登记一次写请求；不在详情编辑器里时原样返回。 */
export function useTrackWrite(): TrackWrite {
  const track = useContext(AssetWriteContext);
  return useCallback(<T,>(work: Promise<T>) => (track ? track(work) : work), [track]);
}
