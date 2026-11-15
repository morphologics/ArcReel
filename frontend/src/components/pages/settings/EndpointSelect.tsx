import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { DiscoveryFormat, EndpointDescriptor, EndpointKey, ImageCap, MediaType } from "@/types";
import { useEndpointCatalogStore } from "@/stores/endpoint-catalog-store";
import { isComfyuiEndpoint, isComfyuiProtocol } from "./customProviderHelpers";

const MEDIA_GROUP_LABEL_KEY: Record<MediaType, string> = {
  text: "endpoint_text_group",
  image: "endpoint_image_group",
  video: "endpoint_video_group",
  audio: "endpoint_audio_group",
};

// Group order is derived from the declaration order of the table above; a new media type only needs one more entry above
// 分组顺序派生自上表的声明顺序，新增媒体类型只需在上方加一条
const MEDIA_ORDER = Object.keys(MEDIA_GROUP_LABEL_KEY) as MediaType[];

function imageCapabilityKey(caps: ImageCap[]): string {
  if (caps.length === 2) return "image_capability_both";
  return caps[0] === "text_to_image" ? "image_capability_t2i" : "image_capability_i2i";
}

interface EndpointSelectProps {
  /** id of the trigger button, for the field label to associate via `htmlFor`. / 触发按钮的 id，供字段标签用 `htmlFor` 关联。 */
  id?: string;
  /** Accessible name of the trigger button when there is no visible field label (e.g. inside a list row). / 没有可见字段标签时（如列表行内）触发按钮的无障碍名称。 */
  "aria-label"?: string;
  value: EndpointKey;
  onChange: (next: EndpointKey) => void;
  /**
   * Model discovery protocol of the host provider. ComfyUI endpoints and ComfyUI-protocol providers are each other's only counterpart
   * (validated both ways on the server, see docs/adr/0081), so the selector lists only the half that can attach; otherwise the user would only
   * find out this one can't attach by hitting a 422 on save.
   *
   * 宿主供应商的模型发现协议。ComfyUI 端点与 ComfyUI 协议供应商互为对方的唯一对手方
   * （服务端双向校验，见 docs/adr/0081），选择器据此只列得上的那一半——否则用户要撞上
   * 保存时的 422 才知道这条挂不上去。
   */
  protocol: DiscoveryFormat;
  disabled?: boolean;
  /** Narrow placement (inside a list row): the trigger button shows only the endpoint name; the request path stays in the dropdown options. / 窄位置（列表行内）：触发按钮只显示端点名称，请求路径留在下拉选项里。 */
  compact?: boolean;
}

/**
 * "Call endpoint" selector for custom provider models: grouped by media type, each item shows the endpoint name, request method and path;
 * image endpoints are also tagged with text-to-image / image-to-image capability. All options come from the endpoint catalog (single source of truth on the backend).
 *
 * 自定义供应商模型的「调用端点」选择器：按媒体类型分组，每项显示端点名称、请求方法与路径，
 * 图片端点另标文生图 / 图生图能力。选项全部来自端点目录（后端单一真相源）。
 */
export function EndpointSelect({
  id,
  "aria-label": ariaLabel,
  value,
  onChange,
  protocol,
  disabled,
  compact = false,
}: EndpointSelectProps) {
  const { t } = useTranslation("dashboard");
  const endpoints = useEndpointCatalogStore((s) => s.endpoints);
  const initialized = useEndpointCatalogStore((s) => s.initialized);
  const loading = useEndpointCatalogStore((s) => s.loading);
  const fetchCatalog = useEndpointCatalogStore((s) => s.fetch);
  const refreshCatalog = useEndpointCatalogStore((s) => s.refresh);

  // Fetch once when the catalog isn't ready; the store short-circuits itself, so repeated mounts are safe. Only if still not ready after one fetch has the fetch failed, and only then offer a retry
  // 目录未就绪时取一次；store 自身有短路，重复挂载安全。取过一次仍未就绪即取失败，才给重试入口
  const [attempted, setAttempted] = useState(false);
  useEffect(() => {
    if (!initialized) void fetchCatalog().finally(() => setAttempted(true));
  }, [initialized, fetchCatalog]);

  const groups = useMemo(() => {
    const wantsComfyui = isComfyuiProtocol(protocol);
    const usable = endpoints.filter((e) => isComfyuiEndpoint(e) === wantsComfyui);
    return MEDIA_ORDER.map((media) => ({ media, options: usable.filter((e) => e.media_type === media) })).filter(
      (group) => group.options.length > 0,
    );
  }, [endpoints, protocol]);

  const label = (endpoint: EndpointDescriptor) => endpoint.display_name ?? t(endpoint.display_name_key);
  const selected = endpoints.find((e) => e.key === value);

  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Select<EndpointKey>
        // An empty value means "this row has no endpoint yet" (host switched to ComfyUI protocol but there's no endpoint to attach); show placeholder text
        // 空值是「这一行还没有端点」（宿主切到 ComfyUI 协议却无端点可挂），显示占位文字
        value={value === "" ? null : value}
        onValueChange={(next) => {
          // Re-selecting the same value isn't a change: the caller decides whether to discard unsaved capability overrides based on whether the endpoint changed
          // 同值重选不算变化：调用方按端点是否变化决定是否作废未保存的能力覆盖
          if (next !== null && next !== value) onChange(next);
        }}
        disabled={disabled || groups.length === 0}
      >
        <SelectTrigger id={id} aria-label={ariaLabel} className="w-full">
          <SelectValue>
            {() =>
              value === "" ? (
                <span className="truncate text-muted-foreground">
                  {t(initialized ? "cp_endpoint_unselected" : "endpoint_catalog_loading")}
                </span>
              ) : selected ? (
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="truncate">{label(selected)}</span>
                  {!compact && (
                    <span className="truncate font-mono text-xs text-muted-foreground">
                      {selected.request_path_template}
                    </span>
                  )}
                </span>
              ) : (
                // The saved endpoint isn't in the current catalog (data drift or temporarily removed by the backend): show the raw key, don't pretend to be the first item
                // 已存的端点不在当前目录里（数据漂移或后端临时移除）：显示原始 key，不冒充第一项
                <span className="truncate font-mono">{value}</span>
              )
            }
          </SelectValue>
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          {groups.map((group) => (
            <SelectGroup key={group.media}>
              <SelectLabel>{t(MEDIA_GROUP_LABEL_KEY[group.media])}</SelectLabel>
              {group.options.map((endpoint) => (
                <SelectItem key={endpoint.key} value={endpoint.key}>
                  <span className="flex min-w-0 flex-col gap-0.5">
                    <span className="truncate">{label(endpoint)}</span>
                    <span className="flex min-w-0 items-baseline gap-1.5 text-xs text-muted-foreground">
                      <span className="font-mono">{endpoint.request_method}</span>
                      <span className="truncate font-mono">{endpoint.request_path_template}</span>
                      {endpoint.image_capabilities && (
                        <span className="shrink-0">· {t(imageCapabilityKey(endpoint.image_capabilities))}</span>
                      )}
                    </span>
                  </span>
                </SelectItem>
              ))}
            </SelectGroup>
          ))}
        </SelectContent>
      </Select>
      {/* When the catalog fetch fails the selector has no options to list, so offer a retry; store.refresh short-circuits in-flight requests / 目录取失败时选择器没有选项可列，给出重试入口；store.refresh 对在途请求短路 */}
      {attempted && !initialized && !loading && (
        <Button variant="link" size="sm" className="self-start" onClick={() => void refreshCatalog()}>
          {t("endpoint_catalog_retry")}
        </Button>
      )}
    </div>
  );
}
