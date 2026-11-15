/** Who triggers the template; `name` is a tool name, user action name or task type, with values closed-declared by the backend. / 模版的触发方；`name` 是工具名、用户操作名或任务类型，取值由后端封闭声明。 */
export interface PromptTemplateTrigger {
  kind: "agent_tool" | "user_action" | "generation_task";
  name: string;
}

/** Prompt template metadata (frontmatter), shared by list and detail. / 提示词模版元数据（frontmatter），列表与详情共用。 */
export interface PromptTemplateMeta {
  id: string;
  category: string;
  title: string;
  description: string;
  /** Production step it belongs to. / 所属制作步骤。 */
  stage: string;
  invoked_by: PromptTemplateTrigger;
  /** Axis name -> applicable axis values; variant families expand by these axis values. / 轴名 → 适用的轴值；变体族按这里的轴值展开。 */
  applies_to: Record<string, string[]>;
  /** Slot name -> description. / 槽位名 → 说明。 */
  slots: Record<string, string>;
  protected: boolean;
  /** `module:Class`; null when there is no structured output. / `module:Class`，无结构化输出时为 null。 */
  output_schema: string | null;
}

export interface PromptTemplateListResponse {
  templates: PromptTemplateMeta[];
}

export interface PromptTemplatePartial {
  name: string;
  source: string;
  /** Locked: no edit entry is offered. / 锁定：不提供编辑入口。 */
  protected: boolean;
  /** Ids of templates referencing it, in registration order. / 引用它的模版 id，按注册顺序。 */
  referenced_by: string[];
}

/** JSON schema node; declares only the fields read when the settings page expands the output structure. / JSON schema 节点，只声明设置页展开输出结构时读取的字段。 */
export interface JsonSchemaNode {
  type?: string;
  description?: string;
  enum?: unknown[];
  properties?: Record<string, JsonSchemaNode>;
  items?: JsonSchemaNode;
  anyOf?: JsonSchemaNode[];
  $ref?: string;
  $defs?: Record<string, JsonSchemaNode>;
}

export interface PromptTemplateDetail {
  template: PromptTemplateMeta;
  source: string;
  /** In order of first reference; variant families expand to all axis values. / 按首次引用顺序；变体族展开为全部轴值。 */
  partials: PromptTemplatePartial[];
  output_schema?: JsonSchemaNode;
}
