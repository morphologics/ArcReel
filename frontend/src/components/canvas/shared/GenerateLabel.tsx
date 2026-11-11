/**
 * A generate button's text: the action ("Generate Image") and, smaller on the same baseline, the model and its
 * estimated cost ("Seedream 5.0 Pro ~$0.04"). The button centres its contents, which leaves two text sizes off a
 * shared baseline; this row aligns them.
 */
export function GenerateLabel({ label, detail }: { label: string; detail?: string | null }) {
  return (
    <span className="flex items-baseline gap-1.5">
      {label}
      {detail && <span className="text-xs font-normal opacity-85">{detail}</span>}
    </span>
  );
}

/** "Seedream 5.0 Pro ~$0.04" from what is known; null when nothing is. */
export function generateDetail(model: string | null | undefined, cost: string | null | undefined): string | null {
  const parts = [model, cost].filter(Boolean);
  return parts.length > 0 ? parts.join(" ") : null;
}
