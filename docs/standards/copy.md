---
paths:
  - "frontend/src/i18n/zh/**"
  - "lib/i18n/zh/**"
  - "website/docs/**"
  - "README.md"
  - "CONTRIBUTING.md"
  - "docs/**"
  - "**/CLAUDE.md"
---

# Chinese copy

Writing style, factual fidelity and typography follow the `tech-doc-style-chinese` skill; this file only records ArcReel's overrides and additions on top of it. Copy categories are determined per paragraph: one file can contain UI copy, user documentation and agent documentation at the same time. Punctuation in the i18n bundles is checked by `frontend/src/i18n/locale-wording.test.ts` and `tests/unit/lib/i18n/test_locale_wording.py`; when reviewing punctuation, only the documentation prose the tests don't cover needs attention.

## General

### Domain concepts use the Chinese terms in `GLOSSARY.md`; terms listed under `_Avoid_` do not appear in the UI

When the same concept goes by different names, users assume they are two different features and go looking for another entry point that doesn't exist; translations into each language become inconsistent as well.

### Numeric ranges are joined with "～"; when the unit is the same, write it once; percentages are written on both ends

For example `2～10 秒` (2–10 seconds), `10%～20%`. A hyphen is easily confused with a minus sign or an en dash.

Allowed when: syntax the user must type verbatim stays unchanged, for example the range shorthand for duration tiers `3-15`.

### Field names before a placeholder and the quotes around raw values stay as they are

For example the `{field}:` in `{field}: 引用路径越界：{path}` ("reference path out of bounds: {path}"), and the quotes on both sides of `'{value}'`. These are field names and raw values from the data, which users and developers need to match against project files; if they are changed to full-width punctuation, it becomes hard to tell what is original text.

### User documentation and agent documentation use written Chinese, avoiding translationese, colloquialism and AI-speak

- **Translationese**: sentence patterns copied from English structure, such as stacked nominalizations, abstract nouns as subjects, "对……进行……" ("to carry out … on …"), consecutive passive "被" sentences, and layer upon layer of "的". Rewrite as short Chinese sentences built around verbs; for example change "保留机器字面量的写法" ("preserve the written form of machine literals") to "保持原样" ("keep as is"), and "多占一次注意力" ("take up one more unit of attention") to "增加阅读负担" ("add to the reading burden").
- **Colloquialism**: chat-style phrasing, such as "换着叫" ("call it this and that") and "不进界面" ("doesn't go into the UI"); change to "用不同叫法" ("use different names") and "不出现在界面上" ("does not appear in the UI").
- **AI-speak**: formulas that carry no actual information and merely read well, such as parallel four-character phrases, openings like "随着……的发展" ("with the development of …"), stock closing phrases, overuse of bold, and decorative headings. Delete the formula and state the information directly.

Keep to normal Chinese when applying writing principles too: principles such as positive phrasing from `writing-for-agents` apply as long as they fit Chinese usage; ordinary phrases such as "不加句号" ("no full stop") are used as usual.

Allowed when: four-character phrases, passive sentences and "进行" in formal contexts are not errors in themselves, and need not be changed when the original is clear; rewriting must not drop facts, conditions or degree of certainty. UI copy is short and its tone may be more colloquial; this rule does not apply to it.

## UI copy

Applies to text users skim while working, including buttons, labels, hints, statuses, errors and event messages, located in `frontend/src/i18n/zh` and `lib/i18n/zh`. Good UI copy lets users see at a glance what to do next, and keeps its meaning when translated into other languages.

### Omit the subject by default; when it's necessary to say who acts, address the user as "你" (you)

For example write "保存后生效" ("takes effect after saving"), not "你保存后生效" ("takes effect after you save"). The actor is the user by default; writing out the subject only adds to the reading burden. Write the subject when the user and the system need to be distinguished, for example "你取消了任务" ("You canceled the task") and "系统已取消任务" ("The system canceled the task"). Always address the user as "你", corresponding to "you" in the English UI.

### Use "请" (please) only when asking the user to perform an action and omitting it would sound abrupt

Placeholders, tooltips, empty states and status text state things directly; for example a placeholder reads "输入项目名称" ("Enter project name"). If every sentence carries "请", the prompts that really need the user to act no longer stand out.

### No full stop on buttons, labels, tooltips and input hints

Text in these places is a name or a short instruction; a full stop only adds visual noise. Explanations and error messages made up of several sentences use full stops as usual.

### Confirm buttons state the result of the action directly

The primary button of a confirmation dialog uses words that state the result, such as "删除" (Delete), "发布" (Publish) and "覆盖" (Overwrite). Users can tell what will happen on click just by looking at the button; dangerous actions especially need this.

### When an action cannot be completed, phrase it as "无法…" (cannot …); use "抱歉" (sorry) only for problems ArcReel itself caused

"无法连接供应商" ("Cannot connect to provider") states the current situation and naturally leads to the next step; when user input does not meet requirements, simply state the requirement. For the structure of failure messages, see the rule "Failure messages state the cause and next step first; the raw text is collapsed" in `general.md`.

Allowed when: task status names such as "生成失败" ("Generation failed") are status labels and are used as usual.

### In-progress states and menu items that need further input use a single "…"

For example "生成中…" ("Generating…"), "导出…" ("Export…"). The UI does not use the "……" prescribed by the skill: a single character doesn't break across lines and takes up less space.

## User documentation

Applies to paragraphs users consult when they come with a task or a question, located in `website/docs`, the README and CONTRIBUTING. Good user documentation lets readers find what they need, complete the task by following the steps, and confirm the result afterwards, without describing capabilities beyond what the product actually does. The form of address is the same as for UI copy.

## Agent documentation

Applies to paragraphs that developers or agents base decisions on, located in `docs/**` and `CLAUDE.md` at every level. Writing style follows the `writing-for-agents` skill.
