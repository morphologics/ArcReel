/**
 * Project data for the tour demo.
 *
 * Pure frontend constants, annotated with the real `ProjectSummary` / `ProjectData` / `EpisodeScript` types --
 * fields drifting as the types evolve surface at compile time, not when the tour reaches some step. This data is not stored in the DB,
 * not written to disk and not in the project list; it is poured into the store on entering the demo workbench and cleared on leaving.
 *
 * Numbers come from a project progressed to "in production": half the episodes shot, one character missing, props still being filled in. The tour explains
 * "what each of these cells in the UI represents", so every category takes a partial value, giving every stat cell something to say.
 *
 * Content is complete only up to episode 1: all 8 episodes have titles, episode 1 has 6 shots. All copy goes through i18n; the structure stays here.
 *
 * 引导演示用的项目数据。
 *
 * 纯前端常量，用真实的 `ProjectSummary` / `ProjectData` / `EpisodeScript` 类型标注 ——
 * 字段随类型演化漂移时会在编译期暴露，而不是在引导跑到某一步时才显形。这份数据不入库、
 * 不落盘、不进项目列表，进演示工作台时灌进 store，离开即随之清空。
 *
 * 数字取自一个推进到「制作中」的项目：分集拍完一半、角色差一个、道具还在补。引导要讲的是
 * 「界面上这些格子分别代表什么」，所以每一类都取不满的值，让统计格都有话说。
 *
 * 内容只完整到第 1 集：8 集都有标题，第 1 集有 6 条分镜。文案全部走 i18n，结构留在这里。
 */

import type { TFunction } from "i18next";
import type {
  CameraMotion,
  Character,
  EpisodeMeta,
  NarrationEpisodeScript,
  NarrationSegment,
  Prop,
  ProjectData,
  ProjectStatus,
  ProjectSummary,
  Scene,
  ShotType,
} from "@/types";
import { demoPlaceholder } from "./demo-placeholder";

/**
 * Identifier of the demo project. It contains an underscore, which backend `PROJECT_NAME_PATTERN` (`^[A-Za-z0-9-]+$`) always rejects,
 * so a real project with the same name can never appear on disk to shadow the demo route.
 *
 * 演示项目的标识。含下划线，后端 `PROJECT_NAME_PATTERN`（`^[A-Za-z0-9-]+$`）永远拒绝，
 * 所以磁盘上不可能出现同名真实项目来遮蔽演示路由。
 */
export const DEMO_PROJECT_NAME = "onboarding_demo";

/** The only episode in the demo with a script. / 演示里唯一有剧本的分集。 */
export const DEMO_SCRIPTED_EPISODE = 1;

/** Script file name of that episode, equal to `EpisodeMeta.script_file` with the `scripts/` prefix removed. / 该分集的剧本文件名，与 `EpisodeMeta.script_file` 去掉 `scripts/` 前缀后一致。 */
const DEMO_SCRIPT_FILE = "E1.json";

// Route params are compared case-insensitively: wouter compiles routes with the `i` flag (see APP_PROJECT_WORKSPACE_PATTERN),
// so case variants like `/APP/Projects/ONBOARDING_DEMO` also hit the workbench route; comparing only with === would fail to
// recognize them as the demo project, and they would instead be treated as a real project name and request a nonexistent project from the backend.
// 路由参数大小写不敏感比较：wouter 编译路由时带 `i` 标志（见 APP_PROJECT_WORKSPACE_PATTERN），
// `/APP/Projects/ONBOARDING_DEMO` 等大小写变体同样会命中工作台路由，若仅用 === 比较则无法
// 识别成演示项目，转而被当成真实项目名向后端请求一个实际不存在的项目。
export function isDemoProject(name: string | null | undefined): boolean {
  return name?.toLowerCase() === DEMO_PROJECT_NAME;
}

const DEMO_STATUS: ProjectStatus = {
  needs_repair: false,
  repair_reason: null,
  assets: {
    character: { total: 4, available: 3, stale: 0 },
    scene: { total: 3, available: 3, stale: 1 },
    prop: { total: 3, available: 2, stale: 0 },
  },
  episodes_summary: { total: 8, scripted: 1, in_production: 1, completed: 0 },
  source_remaining: false,
};

/**
 * Status distribution of the 8 episodes, aligned with the counts in `DEMO_STATUS.episodes_summary`.
 * Only episode 1 has a script in the demo, and the real backend computes only draft for episodes without a script (see
 * `lib/workflow/workflow_state.py::_episode_production_status`) -- so episodes 2-8 cannot be marked
 * scripted/completed, otherwise clicking in shows only a placeholder note and stats would not match content.
 *
 * 8 集的状态分布，与 `DEMO_STATUS.episodes_summary` 的计数对齐。
 * 只有第 1 集在演示里带剧本，真实后端对没有剧本的分集只会算出 draft（见
 * `lib/workflow/workflow_state.py::_episode_production_status`）——第 2-8 集因此不能标成
 * scripted/completed，否则点进去发现只有占位说明，统计与内容对不上。
 */
const EPISODE_STATUSES: NonNullable<EpisodeMeta["status"]>[] = [
  "in_production",
  "draft",
  "draft",
  "draft",
  "draft",
  "draft",
  "draft",
  "draft",
];

/** Skeleton of episode 1's 6 shots: duration, appearance references, shot size and camera movement. Copy is taken from i18n by index. / 第 1 集 6 条分镜的骨架：时长、出场引用、镜别与运镜。文案按 index 从 i18n 取。 */
const SEGMENT_SKELETONS: {
  duration: number;
  segmentBreak: boolean;
  characters: number[];
  scenes: number[];
  props: number[];
  shotType: ShotType;
  cameraMotion: CameraMotion;
  hasStoryboard: boolean;
}[] = [
  {
    duration: 5,
    segmentBreak: false,
    characters: [0],
    scenes: [0],
    props: [],
    shotType: "Long Shot",
    cameraMotion: "Pan Right",
    hasStoryboard: true,
  },
  {
    duration: 4,
    segmentBreak: false,
    characters: [0, 1],
    scenes: [0],
    props: [0],
    shotType: "Medium Shot",
    cameraMotion: "Tracking Shot",
    hasStoryboard: true,
  },
  {
    duration: 6,
    segmentBreak: true,
    characters: [0, 1],
    scenes: [0],
    props: [0],
    shotType: "Medium Long Shot",
    cameraMotion: "Push In",
    hasStoryboard: true,
  },
  {
    duration: 7,
    segmentBreak: false,
    characters: [0],
    scenes: [1],
    props: [],
    shotType: "Extreme Long Shot",
    cameraMotion: "Pedestal Down",
    hasStoryboard: true,
  },
  {
    duration: 5,
    segmentBreak: false,
    characters: [0],
    scenes: [1],
    props: [1],
    shotType: "Close-up",
    cameraMotion: "Static",
    hasStoryboard: false,
  },
  {
    duration: 6,
    segmentBreak: true,
    characters: [0],
    scenes: [1],
    props: [1],
    shotType: "Medium Close-up",
    cameraMotion: "Zoom Out",
    hasStoryboard: false,
  },
];

type DemoT = TFunction<"onboarding">;

const characterNames = (t: DemoT): string[] =>
  [1, 2, 3, 4].map((n) => t(`demo_character_${n}_name`));
const sceneNames = (t: DemoT): string[] => [1, 2, 3].map((n) => t(`demo_scene_${n}_name`));
const propNames = (t: DemoT): string[] => [1, 2, 3].map((n) => t(`demo_prop_${n}_name`));

/** Summary shape used by the lobby demo card. / 大厅演示卡用的摘要形态。 */
export function buildDemoProject(t: DemoT): ProjectSummary {
  return {
    name: DEMO_PROJECT_NAME,
    title: t("demo_project_title"),
    style: t("demo_project_style"),
    style_template_id: null,
    style_image: null,
    thumbnail: null,
    status: DEMO_STATUS,
    // The demo project has no real change history, so the card does not say "updated N days ago". / 演示项目没有真实的修改记录，卡片上不写「几天前更新」。
    last_activity_at: null,
  };
}

/** Full project shape used by the demo workbench. / 演示工作台用的完整项目形态。 */
export function buildDemoProjectData(t: DemoT): ProjectData {
  const characters: Record<string, Character> = {};
  characterNames(t).forEach((name, i) => {
    characters[name] = {
      description: t(`demo_character_${i + 1}_desc`),
      voice_style: t(`demo_character_${i + 1}_voice`),
      // The 4th character has no image yet, matching 3/4 in the stat cell / 第 4 个角色还没出图，对应统计格里的 3/4
      ...(i < 3 ? { character_sheet: demoPlaceholder(name, "character") } : {}),
    };
  });

  const scenes: Record<string, Scene> = {};
  sceneNames(t).forEach((name, i) => {
    scenes[name] = {
      description: t(`demo_scene_${i + 1}_desc`),
      scene_sheet: demoPlaceholder(name, "scene"),
    };
  });

  const props: Record<string, Prop> = {};
  propNames(t).forEach((name, i) => {
    props[name] = {
      description: t(`demo_prop_${i + 1}_desc`),
      // The 3rd prop has no image yet, matching 2/3 in the stat cell / 第 3 个道具还没出图，对应统计格里的 2/3
      ...(i < 2 ? { prop_sheet: demoPlaceholder(name, "prop") } : {}),
    };
  });

  const segmentCount = SEGMENT_SKELETONS.length;
  const episodes: EpisodeMeta[] = EPISODE_STATUSES.map((status, i) => {
    const episode = i + 1;
    const isScripted = episode === DEMO_SCRIPTED_EPISODE;
    return {
      episode,
      title: t(`demo_episode_${episode}_title`),
      // Only episode 1 has a script in the demo; other episodes have titles only, and entering them shows the "demo only covers episode 1" note
      // 只有第 1 集在演示里带剧本；其余分集只有标题，进去会看到「演示只到第 1 集」的说明
      script_file: isScripted ? `scripts/${DEMO_SCRIPT_FILE}` : "",
      status,
      ...(isScripted
        ? {
            script_status: "generated" as const,
            item_count: segmentCount,
            duration_seconds: SEGMENT_SKELETONS.reduce((sum, s) => sum + s.duration, 0),
            storyboards: {
              total: segmentCount,
              available: SEGMENT_SKELETONS.filter((s) => s.hasStoryboard).length,
              stale: 0,
            },
            // Videos have no usable placeholder form (placeholders can only stand in for still images), so no clip is produced in the demo
            // 视频没有可用的占位形态（占位图只能替静态图），演示里一条都没出片
            videos: { total: segmentCount, available: 0, stale: 0 },
          }
        : { script_status: "none" as const }),
    };
  });

  return {
    title: t("demo_project_title"),
    content_mode: "narration",
    style: t("demo_project_style"),
    aspect_ratio: "9:16",
    default_duration: 5,
    overview: {
      synopsis: t("demo_overview_synopsis"),
      genre: t("demo_overview_genre"),
      theme: t("demo_overview_theme"),
      world_setting: t("demo_overview_world"),
    },
    episodes,
    characters,
    scenes,
    props,
    status: DEMO_STATUS,
    generation_mode: "storyboard",
  };
}

/** Script collection for the demo workbench, keyed to `EpisodeMeta.script_file` without the prefix. / 演示工作台用的剧本集合，键与 `EpisodeMeta.script_file` 去前缀后对应。 */
export function buildDemoScripts(t: DemoT): Record<string, NarrationEpisodeScript> {
  const chars = characterNames(t);
  const scns = sceneNames(t);
  const prps = propNames(t);

  const segments: NarrationSegment[] = SEGMENT_SKELETONS.map((skeleton, i) => {
    const n = i + 1;
    const segmentId = `E${DEMO_SCRIPTED_EPISODE}S${n}`;
    return {
      segment_id: segmentId,
      episode: DEMO_SCRIPTED_EPISODE,
      duration_seconds: skeleton.duration,
      segment_break: skeleton.segmentBreak,
      novel_text: t(`demo_shot_${n}_text`),
      characters_in_segment: skeleton.characters.map((idx) => chars[idx]),
      scenes: skeleton.scenes.map((idx) => scns[idx]),
      props: skeleton.props.map((idx) => prps[idx]),
      image_prompt: {
        scene: t(`demo_shot_${n}_image`),
        composition: {
          shot_type: skeleton.shotType,
          lighting: t(`demo_shot_${n}_lighting`),
          ambiance: t(`demo_shot_${n}_ambiance`),
        },
      },
      video_prompt: {
        action: t(`demo_shot_${n}_video`),
        camera_motion: skeleton.cameraMotion,
        ambiance_audio: t(`demo_shot_${n}_audio`),
        dialogue: [],
      },
      generated_assets: {
        storyboard_image: skeleton.hasStoryboard
          ? demoPlaceholder(segmentId, "storyboard")
          : null,
        storyboard_last_image: null,
        grid_id: null,
        grid_cell_index: null,
        // Video and voiceover have no usable placeholder form, so they stay ungenerated in the demo / 视频与配音没有可用的占位形态，演示里保持未生成
        video_clip: null,
        video_thumbnail: null,
        video_uri: null,
        narration_audio: null,
        status: skeleton.hasStoryboard ? "storyboard_ready" : "pending",
      },
    };
  });

  return {
    [DEMO_SCRIPT_FILE]: {
      episode: DEMO_SCRIPTED_EPISODE,
      title: t(`demo_episode_${DEMO_SCRIPTED_EPISODE}_title`),
      content_mode: "narration",
      novel: {
        title: t("demo_project_title"),
        chapter: t(`demo_episode_${DEMO_SCRIPTED_EPISODE}_title`),
      },
      segments,
    },
  };
}
