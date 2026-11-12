import { useCallback, useEffect, useState } from "react";
import { API, type VersionInfo } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";

export type ShotMediaResource = "storyboards" | "videos";
/** An asset sheet's resource type (``{bucket}/{name}.png``). */
export type AssetSheetResource = "characters" | "scenes" | "props" | "products";
export type VersionedResource = ShotMediaResource | AssetSheetResource;

export interface ResourceVersions {
  versions: VersionInfo[];
  currentVersion: number;
}

/** Project file of a shot's current media or an asset's sheet, whose fingerprint changes with each new or restored
 * version. */
export function shotMediaPath(resourceType: VersionedResource, segmentId: string): string {
  if (resourceType === "storyboards") return `storyboards/scene_${segmentId}.png`;
  if (resourceType === "videos") return `videos/scene_${segmentId}.mp4`;
  return `${resourceType}/${segmentId}.png`;
}

// One version list per resource and fingerprint: the strip's menu, the pane's history and its preview share it, and
// a new or restored version (new fingerprint) reads it again.
const cache = new Map<string, Promise<ResourceVersions>>();

function cacheKey(projectName: string, resourceType: VersionedResource, segmentId: string, fp: number | null): string {
  return `${projectName}\0${resourceType}\0${segmentId}\0${fp ?? ""}`;
}

export function loadResourceVersions(
  projectName: string,
  resourceType: VersionedResource,
  segmentId: string,
): Promise<ResourceVersions> {
  const fp = useProjectsStore.getState().getAssetFingerprint(shotMediaPath(resourceType, segmentId));
  const key = cacheKey(projectName, resourceType, segmentId, fp);
  let pending = cache.get(key);
  if (!pending) {
    pending = API.getVersions(projectName, resourceType, segmentId).then((data) => ({
      versions: [...data.versions].sort((a, b) => b.version - a.version),
      currentVersion: data.current_version,
    }));
    pending.catch(() => cache.delete(key));
    cache.set(key, pending);
  }
  return pending;
}

/** Forget a resource's lists (after a restore, before its new fingerprint arrives). */
export function invalidateResourceVersions(projectName: string, resourceType: VersionedResource, segmentId: string) {
  const prefix = `${projectName}\0${resourceType}\0${segmentId}\0`;
  for (const key of cache.keys()) if (key.startsWith(prefix)) cache.delete(key);
}

/** A shot's (or an asset sheet's) version list, newest first; loads when `enabled`. */
export function useResourceVersions(
  projectName: string,
  resourceType: VersionedResource,
  segmentId: string,
  enabled = true,
): { data: ResourceVersions | null; loading: boolean; error: boolean; reload: () => void } {
  const fp = useProjectsStore((s) => s.getAssetFingerprint(shotMediaPath(resourceType, segmentId)));
  const [state, setState] = useState<{ key: string; data: ResourceVersions | null; error: boolean } | null>(null);
  const [reloads, setReloads] = useState(0);
  const key = `${cacheKey(projectName, resourceType, segmentId, fp)}\0${reloads}`;

  useEffect(() => {
    if (!enabled || !segmentId) return;
    let live = true;
    loadResourceVersions(projectName, resourceType, segmentId).then(
      (data) => live && setState({ key, data, error: false }),
      () => live && setState({ key, data: null, error: true }),
    );
    return () => {
      live = false;
    };
  }, [enabled, projectName, resourceType, segmentId, key]);

  const reload = useCallback(() => {
    invalidateResourceVersions(projectName, resourceType, segmentId);
    setReloads((n) => n + 1);
  }, [projectName, resourceType, segmentId]);

  const settled = state?.key === key ? state : null;
  return { data: settled?.data ?? null, loading: enabled && settled === null, error: settled?.error ?? false, reload };
}
