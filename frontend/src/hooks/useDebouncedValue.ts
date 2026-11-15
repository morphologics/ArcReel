import { useEffect, useState } from "react";

/** Publish the input value after a delay of ms milliseconds; commonly used for high-frequency input such as search boxes. / 将输入值延迟 ms 毫秒发布，常用于搜索框等高频输入场景。 */
export function useDebouncedValue<T>(value: T, ms = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(handle);
  }, [value, ms]);
  return debounced;
}
