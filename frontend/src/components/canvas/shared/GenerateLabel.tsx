/** The model and its estimated cost shown after a generate button's action ("Seedream 5.0 Pro", "~$0.04"). */
export interface GenerateDetail {
  model?: string | null;
  cost?: string | null;
}

/**
 * A generate button's text: the action ("Generate Image") and, smaller on the same baseline, the model and its
 * estimated cost. The button centres its contents, which leaves two text sizes off a shared baseline; this row
 * aligns them. When the button is narrow the action and the cost keep their width and the model name ends in "…".
 */
export function GenerateLabel({ label, detail }: { label: string; detail?: GenerateDetail | null }) {
  const model = detail?.model ?? null;
  const cost = detail?.cost ?? null;
  return (
    <span className="flex min-w-0 items-baseline gap-1.5">
      <span className="shrink-0">{label}</span>
      {(model || cost) && (
        <span className="flex min-w-0 items-baseline gap-1 text-xs font-normal opacity-85">
          {model && (
            <span className="min-w-0 truncate" title={model}>
              {model}
            </span>
          )}
          {cost && <span className="shrink-0">{cost}</span>}
        </span>
      )}
    </span>
  );
}

/** The detail from what is known; null when nothing is. */
export function generateDetail(model: string | null | undefined, cost: string | null | undefined): GenerateDetail | null {
  return model || cost ? { model, cost } : null;
}
