import { create } from "zustand";
import { API } from "@/api";
import type { EndpointDescriptor, ImageCap, MediaType } from "@/types";

// ---------------------------------------------------------------------------
// EndpointCatalog: FE-side cache of custom provider endpoint metadata.
// The source of truth is the backend lib/custom_provider/endpoints.py:ENDPOINT_REGISTRY,
// fetched via GET /api/v1/custom-providers/endpoints; the FE no longer hard-codes the endpoint list/paths/media type map.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// EndpointCatalog —— 自定义供应商 endpoint 元数据的 FE 端缓存。
// 真相源在后端 lib/custom_provider/endpoints.py:ENDPOINT_REGISTRY，
// 由 GET /api/v1/custom-providers/endpoints 拉取，FE 不再硬编码 endpoint 列表/路径/媒体类型 map。
// ---------------------------------------------------------------------------

export interface EndpointPath {
  method: string;
  path: string;
}

/** An endpoint's constraints on the two dimensions of size and duration. Only ComfyUI endpoints take non-default values (docs/adr/0082). / 一个端点对尺寸与时长这两维的约束。只有 ComfyUI 端点会取非默认值（docs/adr/0082）。 */
export interface EndpointConstraints {
  sizeFixed: boolean;
  /** Only decides the text of the read-only duration state: this workflow's duration is inherently fixed (frames not bound). / 只决定时长只读态的文案：这份 workflow 时长天生固定（frames 未绑定）。 */
  durationFixed: boolean;
  /** Also a text flag: the empty duration options are because the frame-rate source cannot be read. Both flags false means "frame rate exists, it just cannot convert into whole-second options". / 同为文案位：档位为空的成因是读不到帧率来源。两位都为假即「帧率有、只是换算不出整秒档位」。 */
  durationFrameRateMissing: boolean;
  /** The duration dimension cannot give any options: the options editor is read-only and the project page's duration control is not rendered; this flag is the criterion. / 时长这一维给不出任何档位——档位编辑区只读、项目页的时长控件不渲染，判据取这一位。 */
  durationTierEmpty: boolean;
  nativeResolution: string | null;
}

interface EndpointCatalogState {
  endpoints: EndpointDescriptor[];
  /** key -> media_type, so the component layer does not re-derive it every time. / key → media_type，组件层不再每次重 derive。 */
  endpointToMediaType: Record<string, MediaType>;
  /** key -> { method, path }, for EndpointSelect to show the path prefix. / key → { method, path }，给 EndpointSelect 显示路径前缀。 */
  endpointPaths: Record<string, EndpointPath>;
  /** key -> image capability array (only image endpoints have it; non-image ones do not appear in the map). / key → image capability 数组（仅 image 类 endpoint 有，非 image 不出现在 map 中）。 */
  endpointToImageCapabilities: Record<string, ImageCap[]>;
  /** key -> whether the execution layer passes down the end frame constraint (true only for video); decides whether a last_frame override can force it on. / key → 执行层是否下传尾帧约束（仅 video 类为 true）；决定 last_frame 覆盖能否强制开启。 */
  endpointToEndImageCapable: Record<string, boolean>;
  /** key -> four parameter constraints: whether the endpoint can drive the size / duration dimensions, whether it can give options, and the native resolution when no option is chosen. / key → 参数约束四项：尺寸 / 时长这两维该端点驱不驱动得了、档位给不给得出来，以及不选档位时的原生分辨率。 */
  endpointConstraints: Record<string, EndpointConstraints>;
  loading: boolean;
  initialized: boolean;
  /** Short-circuit: already initialized or loading -> return directly; otherwise trigger one refresh. / 短路：已初始化或加载中 → 直接 return；否则触发一次 refresh。 */
  fetch: () => Promise<void>;
  /** Force refresh (used when the settings page actively refetches). / 强制刷新（设置页主动重拉时用）。 */
  refresh: () => Promise<void>;
}

function deriveMaps(endpoints: EndpointDescriptor[]): {
  endpointToMediaType: Record<string, MediaType>;
  endpointPaths: Record<string, EndpointPath>;
  endpointToImageCapabilities: Record<string, ImageCap[]>;
  endpointToEndImageCapable: Record<string, boolean>;
  endpointConstraints: Record<string, EndpointConstraints>;
} {
  const endpointToMediaType: Record<string, MediaType> = {};
  const endpointPaths: Record<string, EndpointPath> = {};
  const endpointToImageCapabilities: Record<string, ImageCap[]> = {};
  const endpointToEndImageCapable: Record<string, boolean> = {};
  const endpointConstraints: Record<string, EndpointConstraints> = {};
  for (const e of endpoints) {
    endpointToMediaType[e.key] = e.media_type;
    endpointPaths[e.key] = { method: e.request_method, path: e.request_path_template };
    if (e.image_capabilities) {
      endpointToImageCapabilities[e.key] = e.image_capabilities;
    }
    endpointToEndImageCapable[e.key] = e.end_image_capable;
    endpointConstraints[e.key] = {
      sizeFixed: e.size_fixed,
      durationFixed: e.duration_fixed,
      durationFrameRateMissing: e.duration_frame_rate_missing,
      durationTierEmpty: e.duration_tier_empty,
      nativeResolution: e.native_resolution,
    };
  }
  return {
    endpointToMediaType,
    endpointPaths,
    endpointToImageCapabilities,
    endpointToEndImageCapable,
    endpointConstraints,
  };
}

export const useEndpointCatalogStore = create<EndpointCatalogState>((set, get) => ({
  endpoints: [],
  endpointToMediaType: {},
  endpointPaths: {},
  endpointToImageCapabilities: {},
  endpointToEndImageCapable: {},
  endpointConstraints: {},
  loading: false,
  initialized: false,

  fetch: async () => {
    if (get().initialized || get().loading) return;
    await get().refresh();
  },

  refresh: async () => {
    if (get().loading) return;
    set({ loading: true });
    try {
      const res = await API.listEndpointCatalog();
      const {
        endpointToMediaType,
        endpointPaths,
        endpointToImageCapabilities,
        endpointToEndImageCapable,
        endpointConstraints,
      } = deriveMaps(res.endpoints);
      set({
        endpoints: res.endpoints,
        endpointToMediaType,
        endpointPaths,
        endpointToImageCapabilities,
        endpointToEndImageCapable,
        endpointConstraints,
        loading: false,
        initialized: true,
      });
    } catch {
      // Keep initialized=false on failure so components can degrade to a placeholder; the next fetch still retries.
      // 失败时保持 initialized=false，组件可降级显示 placeholder；下次 fetch 仍会重试。
      set({ loading: false });
    }
  },
}));
