import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Film, ImageIcon, Sparkles } from "lucide-react";
import { API } from "@/api";
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";
import type { ModelCandidatesResponse } from "@/types";
import { formatCost } from "@/utils/cost-format";

// The enabled models change rarely: one list per page load.
let candidatesOnce: Promise<ModelCandidatesResponse> | null = null;
function loadCandidates(): Promise<ModelCandidatesResponse> {
  candidatesOnce ??= API.getModelCandidates().catch((err: unknown) => {
    candidatesOnce = null;
    throw err;
  });
  return candidatesOnce;
}

export interface ModelOption {
  pair: string;
  name: string;
  provider: string;
  /** "~$0.45"; null when unpriced. */
  cost: string | null;
  /** Why the model can't be used for this shot; null when it can. */
  reason: string | null;
}

export interface ModelOptions {
  current: string | null;
  models: ModelOption[];
}

function costText(cost: { amount: number | null; currency: string | null } | null | undefined): string | null {
  if (!cost || cost.amount == null || !cost.currency) return null;
  return `~${formatCost({ [cost.currency]: cost.amount })}`;
}

function describe(candidates: ModelCandidatesResponse, pair: string): Pick<ModelOption, "name" | "provider"> {
  const provider = pair.slice(0, pair.indexOf("/"));
  return {
    name: candidates.model_names[pair] ?? pair.slice(pair.indexOf("/") + 1),
    provider: candidates.provider_names[provider] ?? provider,
  };
}

/**
 * Right-click items that (re)generate a shot's media: "<Generate|Regenerate> — <configured model> (~cost)" and
 * "<…> with ›" listing the enabled models with their estimated cost for this shot; models that can't be used are
 * shown disabled with the reason. The options load when the menu opens.
 */
function ModelGenerateItems({
  icon: Icon,
  label,
  withLabel,
  disabled,
  load,
  onGenerate,
}: {
  icon: typeof Film;
  label: string;
  withLabel: string;
  disabled: boolean;
  load: (signal: AbortSignal) => Promise<ModelOptions>;
  /** null: the configured model. */
  onGenerate: (model: string | null) => void;
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  const [options, setOptions] = useState<ModelOptions | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).then(
      (next) => !controller.signal.aborted && setOptions(next),
      () => !controller.signal.aborted && setFailed(true),
    );
    return () => controller.abort();
  }, [load]);

  const current = options?.models.find((m) => m.pair === options.current);
  const others = options?.models.filter((m) => m.pair !== options.current) ?? [];
  return (
    <>
      <DropdownMenuItem disabled={disabled} onClick={() => onGenerate(null)}>
        <Sparkles aria-hidden />
        <span>
          {label}
          {current && <span className="text-muted-foreground"> — {current.name}</span>}
          {current?.cost && <span className="num text-muted-foreground"> {current.cost}</span>}
        </span>
      </DropdownMenuItem>
      <DropdownMenuSub>
        <DropdownMenuSubTrigger disabled={disabled}>
          <Icon aria-hidden />
          {withLabel}
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent className="relative max-h-[70vh] min-w-64 overflow-y-auto">
          {failed ? (
            <DropdownMenuItem disabled>{t("generate_models_load_failed")}</DropdownMenuItem>
          ) : !options ? (
            <DropdownMenuItem disabled>{t("common:loading")}</DropdownMenuItem>
          ) : others.length === 0 ? (
            <DropdownMenuItem disabled>{t("generate_models_none_other")}</DropdownMenuItem>
          ) : (
            others.map((m) => (
              <DropdownMenuItem key={m.pair} disabled={m.reason !== null} onClick={() => onGenerate(m.pair)}>
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="text-sm">
                    {m.name}
                    <span className="text-xs text-muted-foreground"> · {m.provider}</span>
                  </span>
                  {m.reason && <span className="text-xs text-muted-foreground">{m.reason}</span>}
                </span>
                <span className="num ml-3 text-xs text-muted-foreground">{m.cost ?? (m.reason ? "" : "—")}</span>
              </DropdownMenuItem>
            ))
          )}
        </DropdownMenuSubContent>
      </DropdownMenuSub>
      <DropdownMenuSeparator />
    </>
  );
}

/** The video's items: the enabled image-to-video models, priced for the shot's length. */
export function VideoRegenerateItems({
  projectName,
  segmentId,
  scriptFile,
  durationSeconds,
  hasVideo,
  disabled,
  onGenerate,
}: {
  projectName: string;
  segmentId: string;
  scriptFile: string;
  durationSeconds: number;
  hasVideo: boolean;
  disabled: boolean;
  onGenerate: (videoModel: string | null) => void;
}) {
  const { t } = useTranslation("dashboard");
  const load = useCallback(async (signal: AbortSignal): Promise<ModelOptions> => {
    const candidates = await loadCandidates();
    const pairs = candidates.video.buckets.i2v ?? candidates.video.default;
    const quotes = await API.quoteVideoModels(
      projectName,
      segmentId,
      { script_file: scriptFile, duration_seconds: durationSeconds, models: pairs },
      { signal },
    );
    return {
      current: quotes.current,
      models: pairs.map((pair) => {
        const quote = quotes.quotes[pair];
        const reason = !quote
          ? null
          : "unavailable" in quote
            ? t("video_model_unavailable")
            : !quote.duration_supported
              ? t("video_model_duration_unsupported", {
                  seconds: durationSeconds,
                  supported: quote.supported_durations.join(", "),
                })
              : null;
        return { pair, ...describe(candidates, pair), cost: quote && "cost" in quote ? costText(quote.cost) : null, reason };
      }),
    };
  }, [projectName, segmentId, scriptFile, durationSeconds, t]);
  return (
    <ModelGenerateItems
      icon={Film}
      label={t(hasVideo ? "media_regenerate_video" : "media_generate_video")}
      withLabel={t(hasVideo ? "video_regenerate_with" : "video_generate_with")}
      disabled={disabled}
      load={load}
      onGenerate={onGenerate}
    />
  );
}

/** The storyboard image's items: the enabled image models for the shot's lane (image-to-image when it has
 * reference images), priced per image. */
export function StoryboardGenerateItems({
  projectName,
  segmentId,
  scriptFile,
  hasImage,
  disabled,
  onGenerate,
}: {
  projectName: string;
  segmentId: string;
  scriptFile: string;
  hasImage: boolean;
  disabled: boolean;
  onGenerate: (imageModel: string | null) => void;
}) {
  const { t } = useTranslation("dashboard");
  const load = useCallback(async (signal: AbortSignal): Promise<ModelOptions> => {
    const candidates = await loadCandidates();
    const quotes = await API.quoteStoryboardModels(
      projectName,
      segmentId,
      { script_file: scriptFile, models: candidates.image.default },
      { signal },
    );
    // the models offered for the shot's lane (all image models when the lane has no list)
    const pairs = candidates.image.buckets[quotes.lane] ?? candidates.image.default;
    return {
      current: quotes.current,
      models: pairs.map((pair) => {
        const quote = quotes.quotes[pair];
        const reason = quote && "unavailable" in quote ? t("image_model_unavailable_for_shot") : null;
        return { pair, ...describe(candidates, pair), cost: quote && "cost" in quote ? costText(quote.cost) : null, reason };
      }),
    };
  }, [projectName, segmentId, scriptFile, t]);
  return (
    <ModelGenerateItems
      icon={ImageIcon}
      label={t(hasImage ? "media_regenerate_storyboard" : "media_generate_storyboard")}
      withLabel={t(hasImage ? "storyboard_regenerate_with" : "storyboard_generate_with")}
      disabled={disabled}
      load={load}
      onGenerate={onGenerate}
    />
  );
}

export type SheetAssetType = "character" | "scene" | "prop" | "product";

/**
 * An asset sheet's model options: the configured model (name and estimated cost, for the Generate button) and
 * every enabled image model for the sheet's lane, priced. Loads once per asset; null until loaded or on failure.
 */
export function useAssetSheetModels(projectName: string, assetType: SheetAssetType, name: string): ModelOptions | null {
  const { t } = useTranslation("dashboard");
  const [state, setState] = useState<{ key: string; options: ModelOptions | null } | null>(null);
  const key = `${projectName}\0${assetType}\0${name}`;
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const candidates = await loadCandidates();
        const quotes = await API.quoteAssetSheetModels(projectName, assetType, name, candidates.image.default, {
          signal: controller.signal,
        });
        const pairs = candidates.image.buckets[quotes.lane] ?? candidates.image.default;
        const models = pairs.map((pair) => {
          const quote = quotes.quotes[pair];
          const reason = quote && "unavailable" in quote ? t("image_model_unavailable_for_asset") : null;
          return { pair, ...describe(candidates, pair), cost: quote && "cost" in quote ? costText(quote.cost) : null, reason };
        });
        if (quotes.current && !models.some((m) => m.pair === quotes.current)) {
          const quote = quotes.quotes[quotes.current];
          models.push({
            pair: quotes.current,
            ...describe(candidates, quotes.current),
            cost: quote && "cost" in quote ? costText(quote.cost) : null,
            reason: null,
          });
        }
        if (!controller.signal.aborted) setState({ key, options: { current: quotes.current, models } });
      } catch {
        if (!controller.signal.aborted) setState({ key, options: null });
      }
    })();
    return () => controller.abort();
  }, [projectName, assetType, name, key, t]);
  return state?.key === key ? state.options : null;
}

/** The configured model of loaded options: its name and cost ("~$0.04"), for a button label. */
export function currentModel(options: ModelOptions | null): ModelOption | null {
  return options?.models.find((m) => m.pair === options.current) ?? null;
}

/** The asset sheet's right-click items, from options already loaded by useAssetSheetModels. */
export function AssetSheetGenerateItems({
  options,
  hasImage,
  disabled,
  onGenerate,
}: {
  options: ModelOptions | null;
  hasImage: boolean;
  disabled: boolean;
  onGenerate: (imageModel: string | null) => void;
}) {
  const { t } = useTranslation("dashboard");
  const load = useCallback(
    () => (options ? Promise.resolve(options) : Promise.reject(new Error("model options not loaded"))),
    [options],
  );
  return (
    <ModelGenerateItems
      icon={ImageIcon}
      label={t(hasImage ? "asset_regenerate_image" : "asset_generate_image")}
      withLabel={t(hasImage ? "storyboard_regenerate_with" : "storyboard_generate_with")}
      disabled={disabled}
      load={load}
      onGenerate={onGenerate}
    />
  );
}

