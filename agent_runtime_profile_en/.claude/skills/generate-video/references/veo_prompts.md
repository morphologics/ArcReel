# Veo 3.1 Video Generation Prompt Guide

Best practices for writing prompts for Veo 3.1 video generation.

## Prompt structure

Following Veo best practices, a prompt should contain the following elements (blended naturally, without labels):

1. **Composition**: shot type (wide shot, close-up, medium shot)
2. **Subject**: scene description, including characters, environment, objects
3. **Action**: what the characters are doing
4. **Dialogue**: 说话人（语气）说道："台词文本" (Speaker (tone) says: "line text")
5. **Sound Effects**: blended naturally into the scene description
6. **Camera**: a natural description of the movement
7. **Ambiance**: lighting and mood

**Important**: Do not write the following into the prompt (these are passed via API parameters):
- Video duration (e.g. "8 秒" ("8 seconds"))
- Aspect ratio (e.g. "16:9", "9:16")

## Dialogue and audio

### Dialogue format
```
男人（手握猎刀）说道："这不是普通的熊。"
女人（声音因恐惧而紧绷，四处张望）说道："那是什么？"
```

Wrap the dialogue in quotation marks, and describe the action and manner of speaking in parentheses.

### Manner-of-speaking descriptions
- `轻声`, `低语`, `大喊`, `喃喃自语` (softly, whispering, shouting, muttering)
- `温柔地`, `紧张地`, `坚定地` (gently, nervously, firmly)
- `低沉的男声`, `清脆的女声` (a deep male voice, a crisp female voice)

### Sound effects (blended naturally)
Do not use a "音效：" ("Sound effects:") label; describe them naturally instead:
```
一声粗犷的吠叫，树枝折断的声音，潮湿泥土上的脚步声。一只孤鸟鸣叫。
```

```
轮胎尖锐的刹车声，引擎轰鸣。
```

### About BGM
- **Do not describe background music in the prompt**
- The generation side already appends the BGM / subtitle / watermark prohibitions to the end of the prompt automatically; no manual handling needed

## Camera movement

| English term | Chinese description |
|---------|---------|
| static | 镜头静止 |
| pan left/right | 镜头向左/右平移 |
| tilt up/down | 镜头向上/下倾斜 |
| dolly in/out | 镜头缓缓推进/拉远 |
| track left/right | 镜头向左/右跟踪移动 |
| crane up/down | 镜头升起/降落 |
| handheld | 手持镜头轻微晃动 |

## Shot sizes

| English term | Chinese term | Use for |
|---------|---------|---------|
| extreme close-up | 特写 | Emotion, details |
| close-up | 近景 | Faces, dialogue |
| medium shot | 中景 | Upper body, dialogue |
| full shot | 全景 | Full body |
| wide shot | 远景 | Environment, establishing shots |
| aerial | 俯瞰 | Bird's-eye view |

## Negative prompts

When excluding unwanted elements, do not use negative phrasing; list what to exclude directly:
- ❌ Negative language: "no walls"
- ✅ List directly: "walls, frames, borders"

The BGM / subtitle / watermark prohibitions are appended automatically by the generation side at the end of the prompt; no manual handling needed.

## Examples

### Dialogue and atmosphere scene
```
wide shot，迷雾笼罩的太平洋西北森林。两个疲惫的徒步者，一男一女，穿过蕨类植物丛，男人突然停下，盯着一棵树。close-up：树皮上有新鲜的深深的爪痕。男人（手握猎刀）说道："这不是普通的熊。" 女人（声音因恐惧而紧绷，四处张望）说道："那是什么？" 一声粗犷的吠叫，树枝折断的声音，潮湿泥土上的脚步声。一只孤鸟鸣叫。
```

### Detailed description scene
```
close-up 电影镜头跟随一个绝望的男人，他身穿破旧的绿色风衣，在一面粗糙的砖墙上拨打转盘电话，笼罩在绿色霓虹灯诡异的光芒中。镜头缓缓推进，揭示他下巴的紧绷和脸上刻满的绝望，他挣扎着试图打通电话。浅景深聚焦在他紧皱的眉头和黑色转盘电话上，背景模糊成霓虹色彩和模糊阴影的海洋，营造出紧迫感和孤立感。
```

### Animation-style scene
```
一个欢快卡通风格的 3D 动画场景。一只有着雪豹般皮毛、大大的富有表情的眼睛、友好圆润外形的可爱生物，欢快地在异想天开的冬季森林中蹦跳。场景中有圆润的积雪覆盖的树木、轻柔飘落的雪花，以及透过树枝洒下的温暖阳光。生物弹跳的动作和灿烂的笑容传达出纯粹的喜悦。明亮、欢快的色彩和活泼的动画，温馨治愈的基调。
```

### Storyboard-to-video scene
```
一段超现实、电影感的微距视频。小小的冲浪者在石头洗手池里乘着永不停歇的翻滚波浪。一个老式黄铜水龙头正在流水，制造出无尽的海浪。镜头缓缓平移过这个异想天开、阳光明媚的场景，微型角色熟练地在碧绿的水面上划水。
```
