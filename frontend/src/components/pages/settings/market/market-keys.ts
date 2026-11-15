/** Lookup key for install count and rating aggregates: slug is unique within one source. / 安装量与评分聚合的查找键：同一源里 slug 唯一。 */
export function aggregateKey(sourceId: number, slug: string): string {
  return `${sourceId}/${slug}`;
}
