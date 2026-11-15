import type { AssetSheetStatusRow, AssetSheetType, Character, Product, Prop, Scene } from "@/types";

/** Asset shown on a browse card: the four asset types normalized into one shape; type differences only show in the optional counts. / 浏览卡展示的资产：四类资产归一后的同一形状，类型差异只体现在可选的计数上。 */
export interface GalleryAsset {
  type: AssetSheetType;
  name: string;
  description: string;
  /** Asset sheet path registered on the asset entry; unregistered means first generation. / 资产条目上登记的资产图路径；未登记即首次生成。 */
  sheetPath: string | null;
  aliasCount: number;
  derivativeCount: number;
  /** Number of product original images. / 商品原图张数。 */
  referenceCount: number;
}

export type GalleryAssetSource = Character | Scene | Prop | Product;

function sheetPathOf(type: AssetSheetType, asset: GalleryAssetSource): string | undefined {
  switch (type) {
    case "character":
      return (asset as Character).character_sheet;
    case "scene":
      return (asset as Scene).scene_sheet;
    case "prop":
      return (asset as Prop).prop_sheet;
    case "product":
      return (asset as Product).product_sheet;
  }
}

export function toGalleryAsset(type: AssetSheetType, name: string, asset: GalleryAssetSource): GalleryAsset {
  return {
    type,
    name,
    description: asset.description ?? "",
    sheetPath: sheetPathOf(type, asset) || null,
    aliasCount: "aliases" in asset ? (asset.aliases?.length ?? 0) : 0,
    derivativeCount: type === "character" ? Object.keys((asset as Character).derivatives ?? {}).length : 0,
    referenceCount: type === "product" ? ((asset as Product).reference_images?.length ?? 0) : 0,
  };
}

/** Status of the asset sheet itself: the artifact manifest's verdict when present, otherwise inferred from whether the entry has a sheet registered. / 资产图本身的状态：产物清单有判定时以它为准，未取到时按条目上是否登记了资产图推断。 */
export function sheetStateOf(asset: GalleryAsset, row: AssetSheetStatusRow | undefined): "missing" | "stale" | "current" {
  if (row) return row.status === "missing" || row.status === "stale" ? row.status : "current";
  return asset.sheetPath ? "current" : "missing";
}

/** Status marker at the corner of the card image; `current` is not shown. / 卡片图片角上的状态标记；`current` 不显示。 */
export type GalleryMarker = "generating" | "no-description" | "missing" | "stale" | "current";

export function markerOf(asset: GalleryAsset, row: AssetSheetStatusRow | undefined, generating: boolean): GalleryMarker {
  if (generating) return "generating";
  if (!asset.description.trim()) return "no-description";
  return sheetStateOf(asset, row);
}

export type GalleryFilter = "all" | "pending" | "stale";

/** Toolbar filters look only at asset sheet status: pending means no usable sheet yet, stale means the sheet does not match the current description or original images. / 工具栏筛选只看资产图状态：待生成即还没有可用的资产图，过期即资产图与当前描述或原图不一致。 */
export function matchesGalleryFilter(asset: GalleryAsset, row: AssetSheetStatusRow | undefined, filter: GalleryFilter): boolean {
  if (filter === "all") return true;
  const state = sheetStateOf(asset, row);
  return filter === "pending" ? state === "missing" : state === "stale";
}

/** Resource type in the asset sheet version API. / 资产图版本接口里的资源类型。 */
export const VERSION_RESOURCE = {
  character: "characters",
  scene: "scenes",
  prop: "props",
  product: "products",
} as const satisfies Record<AssetSheetType, string>;
