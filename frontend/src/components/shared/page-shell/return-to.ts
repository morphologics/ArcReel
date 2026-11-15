import { useCallback, useEffect } from "react";
import { useLocation, useSearch } from "wouter";

import {
  ROUTE_APP,
  ROUTE_APP_ASSETS,
  ROUTE_APP_PROJECTS,
  ROUTE_APP_SETTINGS,
  WORKSPACE_ROUTE_SETTINGS,
} from "@/app-routes";
import { DEMO_PROJECT_NAME } from "@/onboarding/demo-project";

const RETURN_TO_KEY = "arcreel:returnTo";
/** The last project page visited (path + query): the app's root address reopens it. */
const LAST_PROJECT_KEY = "arcreel:lastProject";

/** Pages whose "Back" returns to where you were before entering. Moving between these pages does not change the return target. / 「返回」回到进入之前位置的页面。在这些页面之间往来不改变返回目标。 */
const RETURN_TO_PAGES = [ROUTE_APP_SETTINGS, ROUTE_APP_ASSETS];

/** Entries that only redirect and never stay. If recorded as the return target, going back would just be redirected again. / 只做重定向、不会停留的入口。记成返回目标的话，返回后又被重定向回来。 */
const REDIRECT_ONLY_PATHS = [`${ROUTE_APP_PROJECTS}/${DEMO_PROJECT_NAME}/${WORKSPACE_ROUTE_SETTINGS}`];

function isAppPath(path: string) {
  return path.startsWith(`${ROUTE_APP}/`);
}

function isReturnTarget(path: string) {
  const normalized = path.toLowerCase().replace(/\/+$/, "");
  if (REDIRECT_ONLY_PATHS.includes(normalized)) return false;
  return !RETURN_TO_PAGES.some((page) => normalized === page || normalized.startsWith(`${page}/`));
}

/**
 * Record the most recent app page visited (including the query string), used by "Back" in global settings and the asset library. Mounted at the router root,
 * so entry buttons, deep links and onboarding jumps do not each have to record their source. The record lives in sessionStorage, survives a page refresh,
 * and does not cross tabs.
 *
 * 记录最近停留的应用页面（含查询串），供全局设置与资产库的「返回」使用。挂在路由根部，
 * 入口按钮、深链与新手引导的跳转都不需要各自记录来源。记录放在 sessionStorage，刷新页面后仍然有效，
 * 不跨标签页。
 */
export function useTrackReturnTo() {
  const [location] = useLocation();
  const search = useSearch();
  useEffect(() => {
    if (!isAppPath(location) || !isReturnTarget(location)) return;
    const target = search ? `${location}?${search}` : location;
    sessionStorage.setItem(RETURN_TO_KEY, target);
    if (isProjectPage(location)) {
      try {
        localStorage.setItem(LAST_PROJECT_KEY, target);
      } catch {
        // storage unavailable: the root address opens the projects list
      }
    }
  }, [location, search]);
}

function isProjectPage(path: string) {
  const name = path.slice(ROUTE_APP_PROJECTS.length + 1).split("/")[0];
  return path.startsWith(`${ROUTE_APP_PROJECTS}/`) && Boolean(name) && decodeURIComponent(name) !== DEMO_PROJECT_NAME;
}

/** Where the app's root address goes: the last project page visited, else the projects list. */
export function appHomeRoute(): string {
  try {
    const last = localStorage.getItem(LAST_PROJECT_KEY);
    if (last && isProjectPage(last.split("?")[0])) return last;
  } catch {
    // storage unavailable
  }
  return ROUTE_APP_PROJECTS;
}

/**
 * Go back to the app page visited before entering the current page; with no record (e.g. the URL opened directly), go to the project lobby.
 * Only for pages on top-level routes: `navigate` in nested routes is relative to the nested path.
 *
 * 返回进入当前页面之前停留的应用页面；没有记录时（如直接打开地址）回到项目大厅。
 * 只用于顶层路由上的页面：嵌套路由里的 `navigate` 以嵌套路径为基准。
 */
export function useReturnTo() {
  const [, navigate] = useLocation();
  return useCallback(() => {
    const target = sessionStorage.getItem(RETURN_TO_KEY);
    navigate(target && isAppPath(target) ? target : ROUTE_APP_PROJECTS);
  }, [navigate]);
}
