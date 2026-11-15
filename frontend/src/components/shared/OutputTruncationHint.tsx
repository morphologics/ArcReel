import { useTranslation } from "react-i18next";
import { Link } from "wouter";

import { buttonVariants } from "@/components/ui/button";
import { customModelSettingsPath, type OutputTruncation } from "@/utils/output-truncation";

/**
 * The way out when a text task's output is truncated: for custom provider models give a "register max output length" link that jumps to this model's entry in settings;
 * for built-in models suggest switching to another text model. Failure feedback of all text tasks shares this one place.
 *
 * 文本任务输出被截断时的出路：自定义供应商的模型给「去登记最大输出长度」链接，跳到设置里这个模型的条目；
 * 内置模型提示换一个文本模型。各文本任务的失败反馈共用这一处。
 */
export function OutputTruncationHint({ truncation }: { truncation: OutputTruncation }) {
  const { t } = useTranslation("dashboard");
  if (!truncation.custom) return <p className="m-0 text-muted-foreground">{t("text_output_truncated_switch_model")}</p>;
  const settingsPath = customModelSettingsPath(truncation.providerId, truncation.model);
  if (settingsPath === null) return null;
  return (
    <Link href={`~${settingsPath}`} className={buttonVariants({ variant: "outline", size: "sm" })}>
      {t("text_output_truncated_register_limit")}
    </Link>
  );
}
