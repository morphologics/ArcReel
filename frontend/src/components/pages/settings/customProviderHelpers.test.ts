import { describe, expect, it } from "vitest";
import type { CapabilityOverrides, ImageCap, MediaType } from "@/types";
import {
  urlPreviewFor,
  toggleDefaultReducer,
  mergeDiscoveredModels,
  withLastFrameOverride,
  withDurationsOverride,
} from "./customProviderHelpers";

// Test fixture: simulates the endpoint->media map derived from endpoint-catalog-store.
// 测试 fixture：模拟从 endpoint-catalog-store 派生的 endpoint→media map。
const ENDPOINT_TO_MEDIA: Record<string, MediaType> = {
  "openai-chat": "text",
  "gemini-generate": "text",
  "openai-images": "image",
  "openai-images-generations": "image",
  "openai-images-edits": "image",
  "gemini-image": "image",
  "openai-video": "video",
  "newapi-video": "video",
  "openai-tts": "audio",
};

const ENDPOINT_TO_CAPS: Record<string, ImageCap[]> = {
  "openai-images": ["text_to_image", "image_to_image"],
  "openai-images-generations": ["text_to_image"],
  "openai-images-edits": ["image_to_image"],
  "gemini-image": ["text_to_image", "image_to_image"],
};

describe("urlPreviewFor", () => {
  it("openai appends /v1 when missing", () => {
    expect(urlPreviewFor("openai", "https://api.example.com")).toBe(
      "https://api.example.com/v1/models",
    );
  });
  it("openai preserves /v1", () => {
    expect(urlPreviewFor("openai", "https://api.example.com/v1")).toBe(
      "https://api.example.com/v1/models",
    );
  });
  it("openai strips trailing slash and appends /v1", () => {
    expect(urlPreviewFor("openai", "https://api.example.com/")).toBe(
      "https://api.example.com/v1/models",
    );
  });
  it("google uses /v1beta/models", () => {
    expect(urlPreviewFor("google", "https://generativelanguage.googleapis.com")).toBe(
      "https://generativelanguage.googleapis.com/v1beta/models",
    );
  });
  it("google strips user-supplied version path", () => {
    expect(urlPreviewFor("google", "https://generativelanguage.googleapis.com/v1beta")).toBe(
      "https://generativelanguage.googleapis.com/v1beta/models",
    );
  });
  it("empty base_url returns null", () => {
    expect(urlPreviewFor("openai", "")).toBeNull();
    expect(urlPreviewFor("google", "  ")).toBeNull();
  });
});

describe("toggleDefaultReducer", () => {
  it("toggles target row and clears siblings within same media_type", () => {
    const rows = [
      { key: "a", endpoint: "openai-chat", is_default: true },
      { key: "b", endpoint: "gemini-generate", is_default: false },
      { key: "c", endpoint: "openai-images", is_default: true },
    ];
    const result = toggleDefaultReducer(rows, "b", ENDPOINT_TO_MEDIA);
    expect(result.find((r) => r.key === "a")?.is_default).toBe(false);
    expect(result.find((r) => r.key === "b")?.is_default).toBe(true);
    expect(result.find((r) => r.key === "c")?.is_default).toBe(true);
  });

  it("treats two image endpoints that declare no capabilities as colliding", () => {
    // ComfyUI endpoints have an empty capability set (capability derivation not landed): two such defaults cannot be told apart and the server would reject them.
    // If the two sides' criteria disagree, the UI would let through a config that cannot be saved.
    // ComfyUI 端点的能力集是空的（能力推导未落地）：两个这样的默认分不开彼此，服务端会拒。
    // 两边判据不一致，界面就会放行一份存不下的配置。
    const rows = [
      { key: "a", endpoint: "ce-1", is_default: true },
      { key: "b", endpoint: "ce-2", is_default: false },
    ];
    const media: Record<string, MediaType> = { "ce-1": "image", "ce-2": "image" };

    const result = toggleDefaultReducer(rows, "b", media, { "ce-1": [], "ce-2": [] });

    expect(result.find((r) => r.key === "a")?.is_default).toBe(false);
    expect(result.find((r) => r.key === "b")?.is_default).toBe(true);
  });

  it("toggling already-default row turns it off", () => {
    const rows = [{ key: "a", endpoint: "openai-chat", is_default: true }];
    expect(toggleDefaultReducer(rows, "a", ENDPOINT_TO_MEDIA)[0].is_default).toBe(false);
  });

  it("falls back to single-row toggle when catalog map is empty (catalog not loaded)", () => {
    // Regression: when the catalog is not loaded endpointToMediaType={}, and every row's mediaType is undefined.
    // It must degrade to a single-row toggle, not treat rows of different media types as one mutually exclusive group because undefined === undefined.
    // 回归：catalog 未加载时 endpointToMediaType={}，所有行 mediaType 都是 undefined。
    // 必须降级为单行 toggle，不能因 undefined === undefined 把不同媒体类型行当作同组互斥。
    const rows = [
      { key: "a", endpoint: "openai-chat", is_default: true },
      { key: "b", endpoint: "openai-images", is_default: true },
      { key: "c", endpoint: "newapi-video", is_default: true },
    ];
    const result = toggleDefaultReducer(rows, "b", {});
    expect(result.find((r) => r.key === "a")?.is_default).toBe(true);
    expect(result.find((r) => r.key === "b")?.is_default).toBe(false);
    expect(result.find((r) => r.key === "c")?.is_default).toBe(true);
  });

  it("falls back to single-row toggle when target endpoint is not in catalog", () => {
    const rows = [
      { key: "a", endpoint: "openai-chat", is_default: true },
      { key: "b", endpoint: "acme-unknown", is_default: false },
    ];
    const result = toggleDefaultReducer(rows, "b", ENDPOINT_TO_MEDIA);
    expect(result.find((r) => r.key === "a")?.is_default).toBe(true);
    expect(result.find((r) => r.key === "b")?.is_default).toBe(true);
  });

  it("split image endpoints with disjoint caps coexist as defaults", () => {
    const rows = [
      { key: "g", endpoint: "openai-images-generations", is_default: false },
      { key: "e", endpoint: "openai-images-edits", is_default: true },
    ];
    const result = toggleDefaultReducer(rows, "g", ENDPOINT_TO_MEDIA, ENDPOINT_TO_CAPS);
    // Setting -generations as default should not clear -edits (capabilities do not overlap)
    // -generations 设为 default，不应清掉 -edits（capability 不交叠）
    expect(result.find((r) => r.key === "g")?.is_default).toBe(true);
    expect(result.find((r) => r.key === "e")?.is_default).toBe(true);
  });

  it("wildcard openai-images clears split image rows when set as default", () => {
    const rows = [
      { key: "w", endpoint: "openai-images", is_default: false },
      { key: "g", endpoint: "openai-images-generations", is_default: true },
      { key: "e", endpoint: "openai-images-edits", is_default: true },
    ];
    const result = toggleDefaultReducer(rows, "w", ENDPOINT_TO_MEDIA, ENDPOINT_TO_CAPS);
    expect(result.find((r) => r.key === "w")?.is_default).toBe(true);
    expect(result.find((r) => r.key === "g")?.is_default).toBe(false);
    expect(result.find((r) => r.key === "e")?.is_default).toBe(false);
  });

  it("two -generations defaults are mutually exclusive (same capability slot)", () => {
    const rows = [
      { key: "g1", endpoint: "openai-images-generations", is_default: true },
      { key: "g2", endpoint: "openai-images-generations", is_default: false },
    ];
    const result = toggleDefaultReducer(rows, "g2", ENDPOINT_TO_MEDIA, ENDPOINT_TO_CAPS);
    expect(result.find((r) => r.key === "g1")?.is_default).toBe(false);
    expect(result.find((r) => r.key === "g2")?.is_default).toBe(true);
  });

  it("disabling an existing default does NOT clear other capability-overlapping defaults", () => {
    // Regression: unsetting the wildcard image default should not also clear split-edits that is also a default
    // (wildcard and -edits overlap in the I2I slot, but the user did not enable a new default, so no exclusive cleanup should trigger)
    // 回归：取消 wildcard image 默认时，不应把同样作为默认的 split-edits 也清掉
    // （wildcard 与 -edits 在 I2I 槽 overlap，但用户并未启用新默认，不该触发互斥清理）
    const rows = [
      { key: "w", endpoint: "openai-images", is_default: true },
      { key: "e", endpoint: "openai-images-edits", is_default: true },
    ];
    const result = toggleDefaultReducer(rows, "w", ENDPOINT_TO_MEDIA, ENDPOINT_TO_CAPS);
    expect(result.find((r) => r.key === "w")?.is_default).toBe(false);
    expect(result.find((r) => r.key === "e")?.is_default).toBe(true);
  });

  it("disabling an existing text default leaves other text defaults untouched", () => {
    const rows = [
      { key: "a", endpoint: "openai-chat", is_default: true },
      { key: "b", endpoint: "gemini-generate", is_default: true },
    ];
    const result = toggleDefaultReducer(rows, "a", ENDPOINT_TO_MEDIA);
    expect(result.find((r) => r.key === "a")?.is_default).toBe(false);
    expect(result.find((r) => r.key === "b")?.is_default).toBe(true);
  });
});

describe("mergeDiscoveredModels", () => {
  const row = (model_id: string, endpoint: string, is_default: boolean) => ({
    model_id,
    endpoint,
    is_default,
  });

  it("keeps existing default and drops discovered default in same media_type (regression)", () => {
    // Existing provider: text default = z-chat. Upstream adds a-chat that sorts earlier, and discovery marks the first item a-chat as default.
    // A naive merge would yield two text defaults -> save rejected by the backend's default_model_conflict. After resolution only the existing default stays.
    // 既存 provider：文本默认 = z-chat。上游新增排序更靠前的 a-chat，发现接口把首项 a-chat 标默认。
    // 朴素合并会得到两个 text 默认 → 保存被后端 default_model_conflict 拒绝。消解后只保留既存默认。
    const prev = [row("z-chat", "openai-chat", true)];
    const discovered = [row("a-chat", "openai-chat", true), row("z-chat", "openai-chat", false)];
    const merged = mergeDiscoveredModels(prev, discovered, ENDPOINT_TO_MEDIA);
    expect(merged.filter((m) => m.is_default).map((m) => m.model_id)).toEqual(["z-chat"]);
  });

  it("lets a discovered default fill an empty media slot", () => {
    // Only a text default exists; discovery adds a video model -> the video slot is empty -> filling a default is allowed
    // 既存仅文本默认；发现新增视频模型 → 视频槽空 → 允许补默认
    const prev = [row("z-chat", "openai-chat", true)];
    const discovered = [row("z-chat", "openai-chat", false), row("v1", "newapi-video", true)];
    const merged = mergeDiscoveredModels(prev, discovered, ENDPOINT_TO_MEDIA);
    expect(merged.find((m) => m.model_id === "z-chat")?.is_default).toBe(true);
    expect(merged.find((m) => m.model_id === "v1")?.is_default).toBe(true);
  });

  it("preserves manually-added models absent from the discovery response", () => {
    const prev = [row("manual", "openai-chat", false), row("z-chat", "openai-chat", true)];
    const discovered = [row("z-chat", "openai-chat", false)];
    const merged = mergeDiscoveredModels(prev, discovered, ENDPOINT_TO_MEDIA);
    expect(merged.map((m) => m.model_id).sort()).toEqual(["manual", "z-chat"]);
    expect(merged.find((m) => m.model_id === "z-chat")?.is_default).toBe(true);
  });

  it("keeps disjoint-capability image defaults (split generations vs edits)", () => {
    // Existing -edits (I2I) default; discovery marks -generations (T2I) default -> capabilities do not overlap -> both kept
    // 既存 -edits(I2I) 默认；发现 -generations(T2I) 标默认 → capability 不交叠 → 两者都保留
    const prev = [row("e", "openai-images-edits", true)];
    const discovered = [
      row("g", "openai-images-generations", true),
      row("e", "openai-images-edits", false),
    ];
    const merged = mergeDiscoveredModels(prev, discovered, ENDPOINT_TO_MEDIA, ENDPOINT_TO_CAPS);
    expect(merged.find((m) => m.model_id === "g")?.is_default).toBe(true);
    expect(merged.find((m) => m.model_id === "e")?.is_default).toBe(true);
  });

  it("makes a discovered wildcard-image default yield to an overlapping existing default", () => {
    // Existing -edits (I2I) default; discovery marks wildcard image (T2I+I2I) default -> the I2I slot is taken -> wildcard yields
    // 既存 -edits(I2I) 默认；发现 wildcard image(T2I+I2I) 标默认 → I2I 槽已占 → wildcard 让出
    const prev = [row("e", "openai-images-edits", true)];
    const discovered = [row("w", "openai-images", true), row("e", "openai-images-edits", false)];
    const merged = mergeDiscoveredModels(prev, discovered, ENDPOINT_TO_MEDIA, ENDPOINT_TO_CAPS);
    expect(merged.find((m) => m.model_id === "e")?.is_default).toBe(true);
    expect(merged.find((m) => m.model_id === "w")?.is_default).toBe(false);
  });

  it("leaves create-mode (empty prev) discovery defaults intact", () => {
    const discovered = [
      row("a-chat", "openai-chat", true),
      row("b-chat", "openai-chat", false),
      row("v1", "newapi-video", true),
    ];
    const merged = mergeDiscoveredModels([], discovered, ENDPOINT_TO_MEDIA);
    expect(merged.filter((m) => m.is_default).map((m) => m.model_id)).toEqual(["a-chat", "v1"]);
  });

  it("does not reconcile when catalog is unloaded (unknown endpoints never claim slots)", () => {
    // Catalog not loaded -> all endpoint media unknown -> no resolution (consistent with toggleDefaultReducer's degradation)
    // catalog 未加载 → 所有 endpoint media 未知 → 不消解（与 toggleDefaultReducer 的降级一致）
    const prev = [row("z-chat", "openai-chat", true)];
    const discovered = [row("a-chat", "openai-chat", true), row("z-chat", "openai-chat", false)];
    const merged = mergeDiscoveredModels(prev, discovered, {});
    expect(merged.filter((m) => m.is_default).map((m) => m.model_id).sort()).toEqual([
      "a-chat",
      "z-chat",
    ]);
  });

  it("keeps multiple manually-added rows with empty model_id (no Map key collision)", () => {
    // The user clicks "Add manually" twice, adding two rows with no model_id (both ""), then clicks fetch models.
    // Deduplicating with a Map keyed by model_id would silently drop one row on the key collision; after the fix both rows should remain.
    // 用户连点「手动添加」加了两行未填 model_id（都为 ""），再点获取模型。
    // 若用 model_id 建 Map 去重，两空行键冲突会静默丢一行；修复后两行都应保留。
    const prev = [
      row("", "openai-chat", false),
      row("", "newapi-video", false),
      row("z-chat", "openai-chat", true),
    ];
    const discovered = [row("z-chat", "openai-chat", false), row("a-chat", "openai-chat", true)];
    const merged = mergeDiscoveredModels(prev, discovered, ENDPOINT_TO_MEDIA);
    expect(merged.filter((m) => m.model_id === "").length).toBe(2);
    // The existing default stays protected; a discovered default of the same media_type yields
    // 既存默认仍受保护，发现的同 media_type 默认让出
    expect(merged.filter((m) => m.is_default).map((m) => m.model_id)).toEqual(["z-chat"]);
  });

  it("create-mode passthrough does not reconcile discovery defaults", () => {
    // create mode passes the discovery response through as is without slot resolution: real discovery has at most one default per media_type
    // (no conflict); here artificial overlapping defaults lock the "do not rewrite discovery" contract, and conflicts are left to backend validation.
    // create 模式原样透传 discovery 响应、不做槽位消解：实际 discovery 每个 media_type 至多
    // 一个默认（不会冲突），此处用人造的重叠默认锁定「不改写 discovery」契约，冲突交后端校验。
    const discovered = [row("w", "openai-images", true), row("e", "openai-images-edits", true)];
    const merged = mergeDiscoveredModels([], discovered, ENDPOINT_TO_MEDIA, ENDPOINT_TO_CAPS);
    expect(merged.filter((m) => m.is_default).map((m) => m.model_id)).toEqual(["w", "e"]);
  });
});

describe("withLastFrameOverride", () => {
  it("跟随（undefined）从字典移除该键，而不是写成 false", () => {
    expect(withLastFrameOverride({ last_frame: true }, undefined)).toBeNull();
    expect(withLastFrameOverride({ last_frame: false }, undefined)).toBeNull();
  });

  it("强制开/关写入显式布尔值", () => {
    expect(withLastFrameOverride(null, true)).toEqual({ last_frame: true });
    expect(withLastFrameOverride(null, false)).toEqual({ last_frame: false });
  });

  it("保留字典里其他维度的覆盖，移除 last_frame 后不清空整个字典", () => {
    const prev = { last_frame: true, first_frame: false } as CapabilityOverrides;
    expect(withLastFrameOverride(prev, undefined)).toEqual({ first_frame: false });
    expect(withLastFrameOverride(prev, false)).toEqual({ last_frame: false, first_frame: false });
  });

  it("不改动入参字典", () => {
    const prev: CapabilityOverrides = { last_frame: true };
    withLastFrameOverride(prev, undefined);
    expect(prev).toEqual({ last_frame: true });
  });
});

describe("withDurationsOverride", () => {
  it("写入档位时保留其余维度的覆盖", () => {
    const prev = { last_frame: false } as CapabilityOverrides;
    expect(withDurationsOverride(prev, [4, 8])).toEqual({ last_frame: false, durations: [4, 8] });
  });

  it("移除（undefined）后字典为空即收敛回 null，回到跟随 workflow 原生时长", () => {
    expect(withDurationsOverride({ durations: [4, 8] }, undefined)).toBeNull();
    expect(withDurationsOverride(null, undefined)).toBeNull();
  });
});
