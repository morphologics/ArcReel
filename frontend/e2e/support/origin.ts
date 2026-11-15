// Screenshot baselines are generated on the default preview port; areas of the page that show the local address match the baseline only on this port.
// 截图基线在默认预览端口上生成，页面上显示本机地址的区域只在这个端口下与基线一致。

/** Default port of the preview server; `E2E_PORT` overrides it. / 预览服务的默认端口，`E2E_PORT` 可覆盖。 */
export const DEFAULT_E2E_PORT = 4173;

/** Whether the page address is on the default preview port. / 页面地址是否在默认预览端口上。 */
export function isDefaultOrigin(url: string) {
  return new URL(url).port === String(DEFAULT_E2E_PORT);
}
