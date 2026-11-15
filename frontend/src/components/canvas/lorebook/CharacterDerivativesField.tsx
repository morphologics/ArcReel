import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Plus } from "lucide-react";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { RetainedEditUnit } from "@/components/shared/edit-unit/RetainedEditUnit";
import { useProjectsStore } from "@/stores/projects-store";
import { errMsg } from "@/utils/async";
import type { CharacterDerivative } from "@/types";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { AssetImageDialog } from "./AssetImageDialog";
import { rejectIfAssetBusy } from "./assetBusyGuard";
import { CharacterDerivativeRow } from "./CharacterDerivativeRow";
import { useTrackWrite } from "./useAssetWrites";
import { useCharacterDerivativeSheets } from "./useCharacterDerivativeSheets";

interface CharacterDerivativesFieldProps {
  projectName: string;
  characterName: string;
  derivatives: Record<string, CharacterDerivative>;
  /** Base look asset sheet path; without it derivatives cannot be generated. / 本体资产图路径；没有时衍生不能生成。 */
  ownerSheet?: string;
  readOnly: boolean;
  /** Another write or base look generation in the details holds a lock. / 详情里其他写入或本体生成占用中。 */
  busy: boolean;
}

/**
 * "Derivatives" section of character details: lists derivatives one by one, with "Add derivative" in the section header. Add, rename and delete run immediately;
 * each entry's appearance change is an independent edit unit saved in its row. Write requests count toward lock state via the details editor's write registry.
 *
 * 角色详情的「衍生」区块：逐条列出衍生，区块头有「新增衍生」。新增、改名、删除立即执行；
 * 每条的外观变化是独立的编辑单元，在所在行保存。写请求经详情编辑器的写入登记计入占用态。
 */
export function CharacterDerivativesField({
  projectName,
  characterName,
  derivatives,
  ownerSheet,
  readOnly,
  busy,
}: CharacterDerivativesFieldProps) {
  const { t } = useTranslation(["assets", "common"]);
  const ownerFp = useProjectsStore((s) => (ownerSheet ? s.getAssetFingerprint(ownerSheet) : null));
  // Re-read derivative image status when the registration or base look image changes (staleness depends on both)
  // 登记或本体图变化时重新读取衍生图状态（过期判定依赖二者）
  const revision = `${JSON.stringify(derivatives)}#${ownerSheet ?? ""}#${ownerFp ?? ""}`;
  const entries = Object.entries(derivatives);
  const { statuses, refresh } = useCharacterDerivativeSheets(projectName, characterName, revision, entries.length > 0);
  // Row identity is independent of the derivative name: after a rename the new name keeps the original row, so unsaved appearance changes in it are not lost;
  // if the old name is later reused by a new derivative it gets a new identity, so the two rows never collide
  // 行标识与衍生名无关：改名后新名称沿用原来那一行，行内未保存的外观变化不丢；
  // 旧名之后被新衍生复用时另发标识，两行不会撞在一起
  const [rowKeys, setRowKeys] = useState<{ byName: Record<string, string>; next: number }>({ byName: {}, next: 0 });
  if (entries.some(([name]) => !(name in rowKeys.byName))) {
    setRowKeys((prev) => {
      const byName = { ...prev.byName };
      let next = prev.next;
      for (const [name] of entries) if (!(name in byName)) byName[name] = `row-${next++}`;
      return { byName, next };
    });
  }
  // Keep the previous image's title during the close animation / 关闭动画期间标题沿用上一张图
  const [viewing, setViewing] = useState<{ src: string; alt: string; title: string; open: boolean } | null>(null);

  const handleRenamed = useCallback((from: string, to: string) => {
    setRowKeys((prev) => {
      const { [from]: key, ...rest } = prev.byName;
      return key === undefined ? prev : { ...prev, byName: { ...rest, [to]: key } };
    });
  }, []);

  // Each derivative is retained independently: external delete or rename replaces only unmodified rows, other rows keep receiving real data.
  // 每条衍生独立保留：外部删除或改名只替换无修改的行，其他行继续接收真实数据。
  const liveRows = new Map(
    entries.flatMap(([name, derivative]) => {
      const key = rowKeys.byName[name];
      return key === undefined ? [] : [[key, { name, derivative }] as const];
    }),
  );
  const [rowOrder, setRowOrder] = useState(() => [...liveRows.keys()]);
  const ordered = new Set(rowOrder);
  const addedKeys = [...liveRows.keys()].filter((key) => !ordered.has(key));
  if (addedKeys.length > 0) setRowOrder([...rowOrder, ...addedKeys]);
  const removeRow = useCallback((key: string) => setRowOrder((prev) => prev.filter((item) => item !== key)), []);

  return (
    <div className="flex flex-col gap-2">
      {rowOrder.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("assets:derivative_hint")}</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">
          {rowOrder.map((key) => {
            const row = liveRows.get(key);
            return (
              <RetainedEditUnit
                key={key}
                identity={row ? key : `${key}:removed`}
                value={row ?? null}
                message={t("assets:editor_derivative_removed")}
              >
                {(shown) => shown ? (
                  <CharacterDerivativeRow
                    projectName={projectName}
                    characterName={characterName}
                    name={shown.name}
                    derivative={shown.derivative}
                    status={statuses[shown.name]}
                    ownerHasSheet={Boolean(ownerSheet)}
                    readOnly={readOnly}
                    busy={busy}
                    onView={(image) => setViewing({ ...image, open: true })}
                    onRenamed={handleRenamed}
                    onSheetsChanged={refresh}
                  />
                ) : <RemovedRow rowKey={key} onRemoved={removeRow} />}
              </RetainedEditUnit>
            );
          })}
        </ul>
      )}
      {readOnly ? null : (
        <AddDerivative projectName={projectName} characterName={characterName} busy={busy} onAdded={refresh} />
      )}
      <AssetImageDialog
        src={viewing?.open ? viewing.src : null}
        title={viewing?.title ?? ""}
        alt={viewing?.alt ?? ""}
        onClose={() => setViewing((prev) => (prev ? { ...prev, open: false } : prev))}
      />
    </div>
  );
}

/** Removed rows whose retention ended, or that had no changes, are withdrawn from the section registry. / 保留结束或原本无修改的移除行从区块登记中清退。 */
function RemovedRow({ rowKey, onRemoved }: { rowKey: string; onRemoved: (key: string) => void }) {
  useEffect(() => onRemoved(rowKey), [rowKey, onRemoved]);
  return null;
}

function AddDerivative({
  projectName,
  characterName,
  busy,
  onAdded,
}: {
  projectName: string;
  characterName: string;
  busy: boolean;
  onAdded: () => void;
}) {
  const { t } = useTranslation(["assets", "common"]);
  const nameId = useId();
  const descriptionId = useId();
  const nameRef = useRef<HTMLInputElement>(null);
  const track = useTrackWrite();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setDescription("");
    setError(null);
  };

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed || submitting) return;
    if (busy) {
      setError(t("assets:derivative_busy_hint"));
      return;
    }
    if (rejectIfAssetBusy("character", projectName, characterName, t, "assets:derivative_busy_hint")) return;
    setSubmitting(true);
    setError(null);
    try {
      await track(
        API.addCharacterDerivative(projectName, characterName, trimmed, description.trim()).then(() =>
          refreshAfterWrite(projectName, t),
        ),
      );
      onAdded();
      reset();
      setOpen(false);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        // Ignore close while submitting, so input is not lost while the request is in flight / 提交中不响应关闭，请求在途时输入不丢
        if (!next && submitting) return;
        // Re-check locks on open: between render and click, the base look may have just been locked elsewhere
        // 打开时复核占用：渲染之后到点击之间，本体可能刚被别处占用
        if (next && rejectIfAssetBusy("character", projectName, characterName, t, "assets:derivative_busy_hint")) return;
        setOpen(next);
        if (!next) reset();
      }}
    >
      <PopoverTrigger render={<Button variant="outline" size="sm" className="self-start" disabled={busy} />}>
        <Plus aria-hidden data-icon="inline-start" />
        {t("assets:derivative_add")}
      </PopoverTrigger>
      <PopoverContent align="start" initialFocus={nameRef} className="w-80">
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={nameId}>{t("assets:derivative_name")}</Label>
            <Input
              ref={nameRef}
              id={nameId}
              value={name}
              disabled={submitting}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("assets:derivative_name_placeholder")}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={descriptionId}>{t("assets:derivative_description")}</Label>
            <Textarea
              id={descriptionId}
              value={description}
              disabled={submitting}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("assets:derivative_description_placeholder")}
              rows={2}
            />
          </div>
          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
          <Button type="submit" size="sm" className="self-end" disabled={submitting || name.trim().length === 0}>
            {submitting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : null}
            {t("common:add")}
          </Button>
        </form>
      </PopoverContent>
    </Popover>
  );
}
