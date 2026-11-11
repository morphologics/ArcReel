import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Film, Sparkles } from "lucide-react";
import { API, type VideoModelQuotes } from "@/api";
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";
import type { ModelCandidatesResponse } from "@/types";
import { formatCost } from "@/utils/cost-format";

// The enabled video models change rarely: one list per page load.
let candidatesOnce: Promise<ModelCandidatesResponse> | null = null;
function loadCandidates(): Promise<ModelCandidatesResponse> {
  candidatesOnce ??= API.getModelCandidates().catch((err: unknown) => {
    candidatesOnce = null;
    throw err;
  });
  return candidatesOnce;
}

interface Options {
  current: string | null;
  models: { pair: string; name: string; provider: string; quote: VideoModelQuotes["quotes"][string] | undefined }[];
}

function costLabel(quote: VideoModelQuotes["quotes"][string] | undefined): string | null {
  if (!quote || "unavailable" in quote || !quote.cost || quote.cost.amount == null || !quote.cost.currency) return null;
  return `~${formatCost({ [quote.cost.currency]: quote.cost.amount })}`;
}

/**
 * Right-click items on a shot's video: "Regenerate video — <configured model> (~cost)" and "Regenerate with ›"
 * listing the enabled image-to-video models with their estimated cost for this shot; models that can't run it or
 * don't take its length are shown disabled with the reason. Loads when the menu opens.
 */
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
  /** null: the configured model. */
  onGenerate: (videoModel: string | null) => void;
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  const [options, setOptions] = useState<Options | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const candidates = await loadCandidates();
        const pairs = candidates.video.buckets.i2v ?? candidates.video.default;
        const quotes = await API.quoteVideoModels(
          projectName,
          segmentId,
          { script_file: scriptFile, duration_seconds: durationSeconds, models: pairs },
          { signal: controller.signal },
        );
        setOptions({
          current: quotes.current,
          models: pairs.map((pair) => ({
            pair,
            name: candidates.model_names[pair] ?? pair.slice(pair.indexOf("/") + 1),
            provider: candidates.provider_names[pair.slice(0, pair.indexOf("/"))] ?? pair.slice(0, pair.indexOf("/")),
            quote: quotes.quotes[pair],
          })),
        });
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      }
    })();
    return () => controller.abort();
  }, [projectName, segmentId, scriptFile, durationSeconds]);

  const current = options?.models.find((m) => m.pair === options.current);
  const currentCost = costLabel(current?.quote);
  const others = options?.models.filter((m) => m.pair !== options.current) ?? [];
  return (
    <>
      <DropdownMenuItem disabled={disabled} onClick={() => onGenerate(null)}>
        <Sparkles aria-hidden />
        <span>
          {t(hasVideo ? "media_regenerate_video" : "media_generate_video")}
          {current && <span className="text-muted-foreground"> — {current.name}</span>}
          {currentCost && <span className="num text-muted-foreground"> {currentCost}</span>}
        </span>
      </DropdownMenuItem>
      <DropdownMenuSub>
        <DropdownMenuSubTrigger disabled={disabled}>
          <Film aria-hidden />
          {t("video_regenerate_with")}
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent className="max-h-[70vh] min-w-64 overflow-y-auto">
          {failed ? (
            <DropdownMenuItem disabled>{t("video_models_load_failed")}</DropdownMenuItem>
          ) : !options ? (
            <DropdownMenuItem disabled>{t("common:loading")}</DropdownMenuItem>
          ) : others.length === 0 ? (
            <DropdownMenuItem disabled>{t("video_models_none_other")}</DropdownMenuItem>
          ) : (
            others.map((m) => {
              const quote = m.quote;
              const reason =
                !quote
                  ? null
                  : "unavailable" in quote
                    ? t("video_model_unavailable")
                    : !quote.duration_supported
                      ? t("video_model_duration_unsupported", {
                          seconds: durationSeconds,
                          supported: quote.supported_durations.join(", "),
                        })
                      : null;
              const cost = costLabel(quote);
              return (
                <DropdownMenuItem key={m.pair} disabled={reason !== null} onClick={() => onGenerate(m.pair)}>
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="text-sm">
                      {m.name}
                      <span className="text-xs text-muted-foreground"> · {m.provider}</span>
                    </span>
                    {reason && <span className="text-xs text-muted-foreground">{reason}</span>}
                  </span>
                  <span className="num ml-3 text-xs text-muted-foreground">{cost ?? (reason ? "" : "—")}</span>
                </DropdownMenuItem>
              );
            })
          )}
        </DropdownMenuSubContent>
      </DropdownMenuSub>
      <DropdownMenuSeparator />
    </>
  );
}
