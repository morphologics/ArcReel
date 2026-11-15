/** Instructions keep the last input per project within the session, not written to the project; the top status bar and the "Episodes" view share the same copy. / 附加指令在会话内按项目保留上一次的输入，不写进项目；顶栏状态条与「分集」视图共用同一份。 */
const lastInstructions = new Map<string, string>();

export function lastInstruction(projectName: string): string {
  return lastInstructions.get(projectName) ?? "";
}

export function rememberInstruction(projectName: string, value: string): void {
  lastInstructions.set(projectName, value);
}
