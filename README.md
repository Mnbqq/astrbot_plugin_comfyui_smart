![:name](https://count.getloli.com/@astrbot_plugin_comfyui_smart?name=astrbot_plugin_comfyui_smart&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# AstrBot 的 ComfyUI 智能绘图插件

让 AstrBot 连接 ComfyUI，用一句自然语言就能出图：

> **/画图 一个白裙少女站在樱花树下**

插件会读取你 ComfyUI 里**真实存在的模型**，识别底模架构（SD1.5 / SDXL / Pony / Flux），
自动匹配正确的工作流模板、分辨率、CFG 与采样器，然后用 LLM 把中文改写成提示词。

---

## 功能

- 🎨 **中文直出**：一句描述即可出图，支持行内参数（比例、种子、步数、LoRA 等）
- 🧠 **智能选型**：LLM 从真实模型清单里挑底模与 LoRA，绝不使用不存在的模型
- 🧩 **架构自适应**：自动识别 SD1.5 / SDXL / Pony / Illustrious / Flux，并匹配对应参数
  （Flux 自动改用 `UNETLoader + DualCLIPLoader + FluxGuidance`、CFG=1.0、忽略负面词）
- 🔎 **节点能力探测**：出图前对照你 ComfyUI 实际安装的节点挑模板，缺自定义节点时直接说清缺哪个，
  而不是把工作流发过去再被服务端拒绝
- 📐 **工作流模板化**：`workflows/*.json` 是真正被加载的数据文件，**可以放自己的 API 格式工作流**
- 🛡 **模型与节点校验**：提交前用 ComfyUI 的真实清单校验，报错翻译成中文人话
- 📊 **统计与画廊**：出图记录、模型调用次数、作品回看
- 💬 **无指令出图（可开关）**：开启后直接说「帮我画一张…」即可，无需输入指令
- 🔐 **权限与配额**：白名单 / 黑名单 / 每日上限 / 冷却，管理员沿用 AstrBot 自带设置
- 🖥 **可视化配置页**：浏览器中配置，保存即写盘

---

## 环境要求

**AstrBot >= 4.26.0**（下界不是拍脑袋写的，是用 `tests/check_api_surface.py` 实测出来的）：

| 用到的 API | 自哪个版本起提供 | 更早版本的行为 |
|---|---|---|
| `astrbot.api.web`（`json_response` / `file_response` / `request`） | v4.26.0 | **无回退**，这是硬下界 |
| `AstrBotConfig.save_config_async` | v4.27.0 | 自动回退到同步的 `save_config()` |
| `Star.logger`（插件专属日志器） | v4.27.3 | 自动回退到全局 `astrbot` logger |

另外需要 Python 3.10+，以及一个可访问的 ComfyUI 实例。

## 快速开始

1. 把插件目录放到 AstrBot 的 `data/plugins/` 下，重启 AstrBot
2. 打开插件配置页，填写 ComfyUI 地址（如 `http://127.0.0.1:8188`）
3. 发 `/画图 你的画面描述`

> **不需要 LLM 也能用**：没有配置对话模型时，插件会直接用你的原话当提示词、用第一个可用底模出图，
> 并在结果里注明。配置了 LLM 才会做中文→英文提示词优化与模型选型。

---

## 指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/画图 <描述>`（别名 `/绘图` `/draw` `/生成图片`） | 生成图片 | 按配置 |
| `/模型列表`（别名 `/模型`） | 查看 ComfyUI 里的模型清单 | 所有人 |
| `/模板列表`（别名 `/工作流`） | 查看已加载的工作流模板与自定义目录 | 所有人 |
| `/状态` | 查看 ComfyUI 连接、设备与队列 | 所有人 |
| `/统计` | 查看出图统计 | 所有人 |
| `/刷新模型` | 重新读取模型清单 | **管理员** |
| `/帮助`（别名 `/comfy帮助`） | 显示帮助 | 所有人 |

### 行内参数

```
/画图 16:9 赛博朋克城市 --seed 42 --steps 30
/画图 一个白裙少女 --size 832x1216 --lora style/anime.safetensors:0.8
/画图 猫 --cfg 5.5 --sampler dpmpp_2m --batch 2 --negative "blurry, lowres"
/画图 一只猫 --model juggernaut
```

| 参数 | 别名 | 说明 |
|------|------|------|
| `--size` | `尺寸` `分辨率` | `1024x1536` 形式 |
| `--ratio` | `比例` | `1:1` `16:9` `9:16` `4:3` `3:4` `3:2` `2:3` `21:9` |
| `--width` / `--height` | `宽` `高` | 单个维度 |
| `--seed` | `种子` | 固定随机种子便于复现 |
| `--steps` / `--cfg` / `--sampler` / `--batch` | `步数` `采样器` `批次` | 采样参数 |
| `--lora` | | `文件名` 或 `文件名:强度`（0~2），也可只写关键词 |
| `--model` | `模型` | 底模关键词，支持省略目录前缀 |
| `--negative` | `负面` | 覆盖负面提示词 |

尺寸会自动对齐到 8 的倍数，并限制在总像素上限内（Flux/SDXL 友好）。

---

## 工作流模板

模板是**数据**，不是代码。内置三个：

| 模板 | 适用 | 说明 |
|------|------|------|
| `sd_checkpoint` | SD1.5 / SDXL / Pony / Illustrious | 单文件底模通用图，参数随架构注入 |
| `flux_checkpoint` | 一体化 Flux 单文件底模 | 模型/文本编码器/VAE 打包在一起的版本 |
| `flux_unet` | 分离权重的 Flux | `diffusion_models` 里的 UNET + `text_encoders` 里的 CLIP + 独立 VAE |

### 架构自适应表

| 架构 | 默认分辨率 | 步数 | CFG | 采样器 / 调度器 | 负面词 |
|------|-----------|------|-----|----------------|--------|
| SD1.5 | 512x768 | 25 | 7.0 | dpmpp_2m / karras | 生效 |
| SDXL | 1024x1024 | 28 | 6.0 | dpmpp_2m / karras | 生效 |
| Pony / Illustrious | 1024x1024 | 28 | 7.0 | dpmpp_2m / karras | 生效（自动加 `score_*` 前缀） |
| Flux | 1024x1024 | 20 | 1.0 | euler / simple | **不生效**（guidance 3.5） |
| 未识别 | 512x768 | 25 | 7.0 | dpmpp_2m / karras | 生效（按 SD1.5 处理） |

识别规则：名字含 `xl`/`juggernaut`/`realvis` → SDXL；含 `pony`/`illustrious`/`noobai` → Pony；
含 `flux` → Flux；含 `sd3` → SD3；**其余按 SD1.5**（社区惯例是 SDXL 系模型名字里都带 `xl`）。
识别不准时用配置里的「强制指定架构」覆盖。

配置页里的「强制使用下面的参数」关闭时，上表优先；打开后改用配置页的数值。

### 放自己的模板

把 **API 格式**的工作流 JSON 放进插件数据目录下的 `workflows/`（配置页「状态」页会显示完整路径），
刷新插件即可。两种写法都支持：

1. **裸工作流**（推荐）：直接是 ComfyUI 导出的 API 格式，顶层是 `{"节点id": {...}}`。
   插件会通过**图结构**推导注入点：从 `KSampler` 的 positive/negative 连线回溯找提示词节点、
   定位尺寸节点、模型加载器、VAE、SaveImage，不依赖节点 id 或标题。
2. **带元信息**：

```json
{
  "name": "my_workflow",
  "arch": "flux",
  "loader": "unet",
  "graph": { "1": { "class_type": "UNETLoader", "inputs": { "unet_name": "x.safetensors" } } }
}
```

`arch` 可为 `sd15` / `sdxl` / `pony` / `flux` / `sd3` 或 `generic`；`loader` 为 `checkpoint` 或 `unet`。

### 注入点是怎么找到的

按优先级依次尝试，前一步失败才走下一步：

1. **图语义推导**：从 `KSampler` 的 `positive` / `negative` 连线向上游回溯，
   找到第一个带文本输入的节点，`FluxGuidance` 这类中间节点会被穿过。
2. **节点标题兜底**：用节点标题（ComfyUI 导出的 `_meta.title`，或顶层 `title`）匹配
   「正面 / 负面 / 尺寸 / 采样 / 保存」等关键词。
   输入键不叫 `text` 的自定义提示词节点（如 `wildcard_text`）靠这一步定位。
3. **显式指定（最可靠）**：在模板清单里用 `bindings` 直接点名，可写节点 id 或标题：

```json
{
  "name": "my_workflow",
  "bindings": {
    "positive": { "title": "正面提示词" },
    "negative": { "node": "13", "input": "wildcard_text" },
    "sampler":  { "node": "5" },
    "model_loader": { "node": "1", "input": "ckpt_name" },
    "save": { "node": "7" }
  },
  "graph": {}
}
```

`bindings` 支持的角色：`positive` `negative` `sampler` `latent` `model_loader`
`vae_loader` `lora_loader` `save` `guidance`。`input` 可省略，插件会按节点结构推断；
写错（节点不存在、标题匹配不到、输入键不存在、角色名不认识）会在**加载时**直接报错，
不会拖到出图才失败。

> 提示词只会写进**节点上真实存在**的输入键，绝不会凭空塞一个 `text` 字段。

### 校验与能力探测

载入时模板会被严格校验（节点 id、`class_type`、`inputs`、连线目标是否存在），
并**剔除不可达节点** —— ComfyUI 会校验整图里每个节点的 `class_type`，
一个残留的、缺自定义节点的孤儿节点会导致整个任务被拒绝。

出图前还会用 `GET /object_info` 拿到你 ComfyUI 已安装的节点清单，优先选择节点齐全的模板；
若选中的模板仍缺节点，会直接告诉你缺哪些，例如：

> 模板 sd_checkpoint 需要以下节点，但你的 ComfyUI 没有安装：SaveImage。

节点清单按 5 分钟缓存，不会拖慢每次出图。

---

## 配置

| 分组 | 说明 |
|------|------|
| `server` | ComfyUI 地址、出图等待上限、轮询间隔、排队补偿上限 |
| `llm_settings` | 是否启用提示词优化、指定 provider、或自定义 OpenAI 兼容接口 |
| `draw_settings` | 兜底出图参数、架构指定、质量词开关、**负面词策略（三档）**、强制 VAE、默认负面词 |
| `output` | 群聊 @ 触发人、附带参数信息、图片保留数量与天数 |
| `permission` | 白名单 / 黑名单 / 每日上限 / 冷却 / 管理员豁免 |
| `agent` | 无指令出图开关 |

> 管理员身份沿用 **AstrBot 自带的管理员设置**，不再需要在插件里单独维护一份名单。

### 无指令出图

打开「高级 → 启用无指令出图」后，对话模型会获得一个 `generate_image` 工具，
用户直接说「帮我画一张雪夜里的白猫」即可触发。需要所用模型支持工具调用（function calling）。
默认关闭，避免影响不希望被插话的群。

---

## 数据与存储

目录：`data/plugin_data/astrbot_plugin_comfyui_smart/`

| 文件 / 目录 | 说明 |
|------|------|
| `catalog.json` | 上次发现的模型清单缓存（ComfyUI 不可达时仍可展示） |
| `stats.json` | 出图记录（含每次出图的完整参数，供画廊详情展示与复现）、用户次数、模型调用次数 |
| `quota.json` | 每日计数与冷却状态（**落盘，重启不清零**） |
| `workflows/` | 你自己的 API 格式工作流模板 |
| `output/` | 生成的图片（按数量与天数双重上限自动清理） |

所有写入采用「临时文件 + 原子替换」，并有并发锁，多人同时出图不会互相覆盖。

---

## 画质调优

画质问题大多不是"插件坏了"，而是模型、分辨率、提示词与 VAE 的搭配问题。按下面的顺序排查最省事：

**1. 先确认架构识别正确。** 出图消息里会显示识别到的架构（如 `sd_checkpoint（sd15）`）。
识别错了就在配置页「强制指定架构」里手工填 `sd15` / `sdxl` / `pony` / `flux` / `sd3`。
**SD1.5 模型被当成 SDXL 跑 1024x1024，是肢体错乱、多手指最常见的原因。**

**2. 手部崩坏。** 按影响从大到小：

- 确认分辨率正确（见上）
- 提高步数：`/画图 你的描述 --steps 30`
- **在描述里写清手的动作**（比事后加负面词有效得多）：
  `/画图 少女双手抱胸靠在窗边`、`/画图 女孩一手举伞一手提包`
- 保持 `negative_mode = merge`（手部规避词会自动合并进来）
- 换一只更擅长画手的底模，或加一个手部修正 LoRA

**3. 出图偏色、发灰、发绿。** 基本都是底模自带 VAE 的问题：

1. 下载 `vae-ft-mse-840000-ema-pruned.safetensors`（SD1.5 通用 VAE）
2. 放进 ComfyUI 的 `models/vae/`
3. 配置页「强制使用指定 VAE」填该文件名

**4. 颜色/构图每次都不一样。** 这是随机种子的正常表现，不是故障。
锁定种子即可复现：`/画图 你的描述 --seed 12345`；出图消息与画廊里都会显示本次种子。

**5. 提示词太长没效果。** CLIP 只取前 77 个 token，超出部分会被截断。
把最关键的词放前面（插件已把质量词与手部规避词放在最前），别把核心描述堆在末尾。

**6. 想看某张图到底用了什么词与参数。** 配置页「统计」页的作品画廊里**点击图片**即可查看并可复制。

## 出图失败时怎么排查

插件会尽力把 ComfyUI 的拒绝原因翻译成中文，同时**把服务端的原始响应写进日志**，例如：

```
提交失败：Prompt outputs failed validation
　· Return type mismatch between linked nodes: model, MODEL != CLIP
　· 节点 8（LoraLoader）：lora_name 取值 'ghost.safetensors' 不存在（可用项：a.safetensors、b.safetensors）；缺少必填输入 clip
```

若 ComfyUI 只回了一句 `Prompt outputs failed validation` 而没给节点级原因，
插件会直接告诉你去哪找：

```
提交失败：Prompt outputs failed validation
　· ComfyUI 未返回节点级原因。请查看 ComfyUI 控制台里
　　 “Failed to validate prompt for output” 附近的日志，那里有完整原因。
```

出图前还会先做一次能力探测，缺自定义节点时**根本不会提交**，直接报出缺哪个节点。

### 提交前的本地预检

插件会用你 ComfyUI 自己的 `/object_info` 约束，在**提交之前**本地校验一遍会注入的参数
（下拉取值是否在可选项里、数值是否越界），把最常见的拒绝原因提前拦下并说清：

```
提交前的本地校验未通过（按你 ComfyUI 的输入约束检查）：
　· 节点 5（KSampler）的 sampler_name 取值 'dpmpp_2m' 不存在（可用项：euler、dpmpp_2m_sde）
　· 节点 5（KSampler）的 steps=0 小于最小值 1
　· 本次工作流已保存到 …/last_failed_prompt.json
```

### 服务端没给原因时怎么办

个别 ComfyUI 构建（含各类整合包）在拒绝出图时会返回
`{"error": {"type": "prompt_outputs_failed_validation", "details": ""}, "node_errors": {}}`
—— 既没有节点级原因也没有细节，从报错本身完全无法排查。遇到这种情况插件会：

1. **把实际提交的工作流完整落盘**到数据目录的 `last_failed_prompt.json`
   （含时间、插件版本、ComfyUI 地址、错误原文与 `graph`），可直接发出来定位；
2. 在日志里记录**正向提示词长度**、所用模板、底模与 LoRA；
3. 提示你去看 ComfyUI 控制台里 `Failed to validate prompt for output` 附近的日志。

用 `/状态` 还能看到 ComfyUI 的版本号，便于确认是否为分支构建。

启动时插件会打印一条横幅，用于确认「跑的到底是哪一版」：

```
ComfyUI 智能绘图 v0.3.7 已激活｜模板 3 个｜数据目录 …｜插件专属日志 可用｜Pages 已注册｜排队补偿上限 10 个任务
```

## 常见问题

| 现象 | 处理 |
|------|------|
| 「没有发现任何模型」 | 确认配置页里的 ComfyUI 地址正确，且 `models/` 下有模型；点「刷新模型」 |
| 「提交失败：节点 N 的 ckpt_name 取值 … 不存在」 | ComfyUI 里确实没有该模型，插件已把可用项列出来了 |
| 「提交失败：缺少自定义节点：XXX」 | ComfyUI 端缺少对应自定义节点，或工作流里有残留的孤儿节点 |
| 「提交失败：Prompt outputs failed validation」 | 按报错里给的方向查；若没给节点级原因，看 ComfyUI 控制台 `Failed to validate prompt for output` 附近的日志。插件也已把原始响应写进 AstrBot 日志 |
| 想知道当前跑的是哪一版 | 看插件激活时的那条启动横幅（含版本号、模板数与 Pages 状态） |
| 配置页/状态页报「未找到该路由」 | v0.3.1 已修复。若仍出现，看启动横幅里 `Pages` 是「已注册」还是「注册失败」，后者请把日志里的报错发出来 |
| 模型数量看起来虚高 | v0.3.1 起只统计真正的模型目录。若仍然异常，用 `/模型列表` 看是哪个目录条目多 |
| 报错里 `node_errors` 与 `details` 都是空的 | v0.3.2 起会先把**实际提交的工作流**存到 `last_failed_prompt.json`，并把提示词长度等写进日志。把这个文件和 `/状态` 的输出发出来即可定位 |
| 怀疑是 LLM 选的参数有问题 | 用行内参数直接指定绕过 LLM：`/画图 你的描述 --steps 28 --cfg 6 --sampler dpmpp_2m` |
| 出图分辨率/CFG 明显不合适 | 说明架构识别不准。在配置页「强制指定架构」填 `sd15` / `sdxl` / `pony` / `flux` / `sd3`；出图消息里也会显示识别结果 |
| 手部崩坏、多手指 | 插件已强制合并手部规避词。仍明显时可调高步数（`--steps 30`）、确认架构识别正确（SD1.5 别用 1024 档），并在描述里写清手部动作如「双手抱胸」 |
| 出图偏色、发灰、发绿 | 多为底模自带 VAE 有问题。把 `vae-ft-mse-840000-ema-pruned.safetensors` 放进 ComfyUI 的 `models/vae`，然后在配置页「强制使用指定 VAE」填该文件名 |
| 想看某张图的提示词和参数 | 配置页「统计」页的作品画廊里**点击图片**，弹窗里可看可复制 |
| 「出图超时」 | 提高「出图等待上限」；若前方排队任务多，插件已按队列长度自动补偿等待时间 |
| 「任务已结束但没有图片输出」 | 工作流里需要保留 `SaveImage` 节点 |
| 出图用的是第一个模型而不是想要的 | 用 `--model 关键词` 指定，或在 LLM 设置里指定 provider 以获得更好选型 |
| Flux 出图忽略了负面提示词 | 这是 Flux 的正常行为（CFG=1.0），请改用正向提示词或 `guidance` |

---

## 后续计划

> 插件的能力来自「模板 + 探测」，所以下面大部分功能主要是**新增工作流模板**，
> 而不是改代码。缺失的模型或自定义节点会在出图前被探测出来并明确告知，不会静默失败。

### 近期

| 目标 | 说明 | 前置条件 |
|---|---|---|
| **提取图片的提示词（反推）** | 发一张图或回复一张图，自动反推出可用的英文 tag，可直接接 `/画图` 复现或改写；也能用来搞清楚别人那张图大概是怎么写出来的 | 需要一个支持看图的对话模型（AstrBot 的 `llm_generate` 已支持 `image_urls`，插件侧无需额外依赖） |
| **图生图** | 以你发的图为底按描述改写（如「把这张图改成冬天」），重绘幅度可调 | 用核心节点即可（`LoadImage` + `VAEEncode`），需新增 img2img 模板 |
| **Hires Fix / 放大** | 先出小图再放大重绘。**对手部与人脸的改善通常比调负面词明显得多** | 核心 `LatentUpscale` 即可；`UltimateSDUpscale` 效果更好但需自定义节点 |
| **真实进度与队列** | WebSocket 实时进度（第 n / 总步数）、排队位置、`/取消` 当前任务 | 无 |
| **并发与队列治理** | 限制同时出图数，避免多人同时出图把显存打满 | 无 |

### 中期

| 目标 | 说明 | 前置条件 |
|---|---|---|
| **改图：重绘幅度** | `--denoise 0.4` 这类，同一张图微调而不是重画 | 与图生图同源 |
| **改图：扩图** | 把画面往外扩（outpaint），补全被裁掉的构图 | 需具备 outpaint 能力的工作流 |
| **改图：局部重绘** | 只重画指定区域 | 需要遮罩。聊天里画遮罩不方便，计划在插件页做一个简单的涂抹工具 |
| **多后端调度** | 配置多个 ComfyUI 地址，按负载与健康度自动分配 | 无 |
| **UI 格式工作流自动转换** | 直接导入 ComfyUI 界面导出的工作流（目前只支持 API 格式） | 需实现 UI→API 图转换（连线表与 `widgets_values` 映射） |

### 较远

| 目标 | 说明 | 前置条件 |
|---|---|---|
| **文生视频** | 按描述生成短视频并直接发到聊天 | **需要视频模型**（Wan / HunyuanVideo / LTX-Video 等）与对应自定义节点。8.5 GB 显存建议低分辨率、短时长；14B 级模型大概率放不下 |
| **图生视频** | 以一张图为起点生成动态视频（可指定首尾帧） | 同上，另需 `LoadImage` |
| **视频参数控制** | 时长、帧率、分辨率、首尾帧 | 同上 |
| **国际化** | 界面与提示词多语言（`.astrbot-plugin/i18n`） | 无 |

> **关于视频**：能不能做**主要取决于你 ComfyUI 里装了什么模型与节点**，不是插件单方面能决定的。
> 插件会先探测节点与模型是否齐备，缺什么会直接说清楚。

有想优先要的功能，或者上面没列到的，欢迎开 Issue 提。

## 更新日志

**v0.3.7** — 负面词归一化去重 + 长度诊断 + `raw` 档

- **归一化去重**：`((extra limbs))`、`(extra limbs)`、`extra limbs`、`[[extra limbs]]`
  在 ComfyUI 里是同一个词，此前会被当成四个不同标签全部保留。现在按「剥掉括号与显式权重后」
  的键去重，并保留**权重最高**的写法。实测某份流行长负面词（Deep Negative 系列）
  从 **99 个标签 / 约 435 token / 约 6 段编码** 降到 **46 个 / 约 230 token / 约 3 段**，
  且不丢任何**不同的**词。
- **长度诊断**：日志记录每次正向/负向提示词的长度；负面词超过约 3 段编码时在出图消息里提示精简。
  顺便纠正一个常见误解 —— ComfyUI 对超长提示词**不是截断**，而是切成多段编码后拼接
  （已对照 `comfy/sd1_clip.py` 的 `torch.cat(embeds_out)` 确认），所以长负面词依然生效，
  但会被稀释。
- **新增 `raw` 档**：严格原样使用你的负面词，连去重都不做。
- **默认负面词精简**：从 33 个标签（含 `normal quality`、`error` 等低效项）改为 32 个
  覆盖更准的标签，手部与肢体相关词更集中。

**v0.3.6** — 负面提示词策略可完全自定义

新增配置项 `draw_settings.negative_mode`（配置页「出图」页的下拉框），三档可选：

| 档位 | 效果 | 适用 |
|---|---|---|
| `merge`（默认） | 默认词 + 手部/肢体规避词 + 架构附加词 + LLM 的负面词，合并去重 | 最稳，推荐先用它 |
| `guard_only` | 你的默认词 + 手部/肢体规避词；**不采纳 LLM 的负面词** | 想自己掌控，但保留安全网 |
| `custom_only` | **只用你自己的词**（默认词 + 行内 `--negative`），不追加任何内容，只做重复写法合并 | 想完全自己掌控 |
| `raw` | **严格原样**，连去重都不做，一个字符都不改 | 要绝对确定性 |

行内 `--negative "..."`：在 `merge` / `guard_only` / `custom_only` 下与其它词合并，在 `raw` 下直接覆盖。

### 去重规则

同一个词的**不同括号写法**会被视为同一个词并只保留**权重最高**的那种：

```
((extra limbs))  ==  (extra limbs)  ==  extra limbs  ==  [[extra limbs]]
```

流行的长负面词（如 Deep Negative 系列）里同一个词常被写五六遍（`extra limbs` 6 次、
`ugly` 6 次、`out of frame` 5 次…），去重能把它从 99 个标签压到 46 个而**不丢任何不同的词**。
想完全不改动就选 `raw`。

### 关于「负面词写太长会失效吗」

**不会失效。** ComfyUI 对超出 77 token 的提示词不是截断，而是**切成多段分别编码后
拼接**（`sd_clip.process_tokens` 里的 `torch.cat(embeds_out)`，带 attention mask）。
但每段都要插入 start/end 与 padding 填充，词越多、单个词的相对影响力就越低 ——
这是「负面词写了一百个反而没感觉」的真正原因。

插件会在日志里记录每次的提示词长度，负面词超过约 3 段编码时还会在出图消息里给出精简提示：

```
ℹ️ 负面词较长（99 个标签 / 约 435 token / 约 6 段 CLIP 编码）。过长的负面词不会失效，
   但会稀释每个词的影响力，可考虑精简重复项
```

> 注：这里的分段数是**估算**（没有 CLIP 的 BPE 词表无法精确计数），用于判断是否需要精简足够了。

> ⚠️ 需要说明取舍：`bad hands`、`extra fingers`、`mutated hands` 这些词正是改善手部崩坏的机制之一，
> 完全去掉（`custom_only`）手可能更差。想两者兼顾就用 `guard_only`。

**v0.3.5** — 修复画廊页报错，并给前端补上真正的执行测试

- **修复「统计读取失败：gallery is not defined」**。v0.3.4 把变量 `gallery` 改名为
  `galleryItems` 时漏改了判断条件里的一处引用，导致统计页整页报错。
- **补上前端执行测试** `tests/test_pages_js.js`。之前 `app.js` 只有 `node --check` 做过
  语法检查，而语法检查**查不出「引用了已改名或不存在的变量」**这类错误 —— 这正是本次
  事故能溜过去的原因。现在该测试用最小 DOM 桩把 `app.js` 真正跑起来，驱动
  `loadStats` / `openDetail` / `copyField`，断言画廊条目数、最新一张排在最前、
  弹窗填入正/负面提示词与参数表、老记录（无 `params`）仍能打开、
  剪贴板不可用时退化为选中文本、弹窗可关闭。
  已用「把修复回退」反向验证：它会精确复现 `统计读取失败：gallery is not defined`。

**v0.3.4** — 画质优化 + 画廊详情

针对「手穿模、多手指、颜色变化」这类画质问题，找到并修掉了三个具体成因：

- **负面提示词曾被整体替换**。旧逻辑是「LLM 返回了负面词就整体覆盖默认词」，
  于是 `bad hands`/`extra fingers` 这些手部规避词被悄悄丢掉 —— 这正是多手指的直接来源。
  现在改为**合并**（默认词 + 手部肢体规避词 + 架构附加词 + LLM 词 + 行内参数，去重保序），
  任何一环都挤不掉前面的保护词；可在配置里关掉这个合并。
- **LLM 给的尺寸会绕过架构适配**。提示词里告诉 LLM「默认尺寸 1024x1024」，
  而 LLM 返回的 width/height 优先级高于架构档案，于是 SD1.5 模型又会被拉回 1024 档
  —— 而 SD1.5 在错误分辨率下正是多手指、肢体错乱的高发区。
  现在 LLM 的尺寸**只当比例意图**，总像素按所选架构的预算归一（横构图 1344x768
  会变成 824x472 这类 SD1.5 友好档位）；行内 `--size/--ratio` 仍原样生效。
- **指定的 VAE 曾被静默忽略**。`sd_checkpoint` 用底模自带 VAE、没有 `VAELoader` 节点，
  而注入逻辑写的是「模板有 VAELoader 才写入」，所以配置里的强制 VAE 和 LLM 选中的 VAE
  都不生效。现在会自动插入 `VAELoader` 并改接 `VAEDecode` —— 出图偏色发灰时可据此纠正。

另外：按架构自动补质量词（SD1.5/Pony 加，SDXL/Flux 不加，避免反效果）、
Pony 系自动补 `score_6, score_5, score_4` 低分档排除词、
提示词里要求 LLM 明确描述手部动作（`hands on hips` 这类，比事后加负面词更有效）。

**画廊详情**：作品画廊的图片现在可以点击，弹窗展示大图、完整正/负面提示词
与全部参数（底模、模板、架构、分辨率、步数、CFG、采样器、种子、LoRA、VAE、耗时、发起人），
并带「复制正向 / 复制负面」按钮（iframe 内剪贴板被拒时自动退化为选中文本）。

**v0.3.3** — 修复「选到 LoRA 就出图失败」的根因（在真实服务器上复现并验证）

- **选中 LoRA 时提交的工作流会形成依赖环，被 ComfyUI 直接拒绝**，而且只回一句
  `prompt_outputs_failed_validation`（`details` 与 `node_errors` 都是空的），几乎无法排查。
  根因是 `_apply_lora` 在插入 `LoraLoader` 后，重新连接下游的循环**把刚插入的节点自己也算了进去**，
  把它的 `clip` 改写成了指向自身：

  ```
  修复前: "8": {"class_type": "LoraLoader", "inputs": {"clip": ["8", 1], ...}}   # 指向自己
  修复后: "8": {"class_type": "LoraLoader", "inputs": {"clip": ["1", 1], ...}}   # 指向底模
  ```

  这解释了「短提示词正常、长提示词报错」：提示词越长，LLM 越容易挑一个 LoRA。
  已在真实 ComfyUI 上验证——修复前 HTTP 400，修复后 HTTP 200 并成功出图。
  另外给 `validate_graph` 加了**自环检查**，这类错误以后会在本地就被拦下。
- **SD1.5 模型放在 `diffusion_models` 目录时会被套上 Flux 模板**，提交一堆目标服务器上
  根本不存在的 `text_encoders`/`vae` 取值。现在模板与架构必须相容，不相容时直接给出
  可操作的中文提示，而不是提交坏图。
- **架构识别大幅改进**。原来未识别时兜底 `sdxl`，实测在某用户的 121 个底模上把 115 个
  判成 SDXL（其中绝大多数其实是 SD1.5 时代模型），导致 SD1.5 模型被套上 1024x1024/cfg 6.0
  而明显劣化。现在规则是：带 `xl`/`juggernaut`/`realvis` 判为 SDXL，
  `pony`/`illustrious`/`noobai` 判为 Pony，`flux` 判为 Flux，其余**兜底 SD1.5**；
  同一批模型上识别结果从 115 sdxl 变为 **101 sd15 / 18 sdxl / 2 pony**，且判为 SDXL 的
  名字里确实都带 `xl`。另外新增 `draw_settings.arch_override` 可在配置页手工指定架构。

**v0.3.2** — 让「服务端不说原因」的失败变得可排查

背景：用户实测时拿到 `{"error": {"type": "prompt_outputs_failed_validation",
"details": ""}, "node_errors": {}}` —— 两个字段都是空的，报错本身没有任何可用信息。
对照 ComfyUI 上游 `execution.py` 逐行追过 `validate_prompt` / `validate_inputs`，
确认**按上游代码这个组合在逻辑上不可达**（任何节点错误都会带上 reasons，
除非它是「上游失败」的级联，而级联的根节点必然有 reasons），
因此判断是对方的 ComfyUI 构建存在差异。与其继续猜，改为让下一次失败自证：

- **提交前本地预检**：用服务端自己的 `/object_info` 约束校验注入的参数
  （下拉取值、数值上下界），提前拦下并给出精确报错；这些正是出图被拒的常见原因
- **失败工作流落盘**：`last_failed_prompt.json` 记录实际提交的完整 `graph`
  与错误原文，可直接用于定位
- **失败日志补全**：记录正向提示词长度、模板、底模、LoRA
- **`/状态` 显示 ComfyUI 版本**，便于识别整合包/分支构建

**v0.3.1** — 修复三个实际使用中暴露的问题

- **Pages 全部接口返回「未找到该路由」**（配置页/模型页/状态页不可用）。
  根因：重写时漏了在主模块里调用 `register_pages_routes`，而测试里手动调用了一次，
  正好把这个漏接遮住了。现已改为**构造插件时自动注册**，并加了回归守卫
  （不再手动调用，直接断言构造即注册）；已用「临时移除该调用」反向验证守卫生效。
- **模型数量虚高（例如 2935 个）**。根因：ComfyUI 的 `GET /models` 返回的是
  `folder_names_and_paths` 的**全部**键，其中 `custom_nodes` 不是模型目录，
  且它注册时的扩展名白名单是**空列表**，而 `filter_files_extensions` 对空列表放行所有文件，
  于是递归 custom_nodes 会把每个自定义节点包里的 `.py/.js/node_modules` 全列出来。
  现已改为**模型目录白名单**，并排除 `custom_nodes` / `configs` / `datasets` /
  `embeddings`（文本反演，工作流用不到）/ `vae_approx`（预览用小模型）。
- **顺带清掉两处「写了却从没接上」的死代码**（`ComfyUI.has_node`、
  `workflow_templates._links_of`），并新增**死代码守卫测试**，
  静态检查所有函数是否真的被调用（框架回调与 `@filter.*` 处理器除外），
  从机制上防止这类「半死配置」问题再出现。
- 模板改为在 `__init__` 中加载，不再依赖 `initialize()` 的调用时序。

**v0.3.0** — 架构级重写

修复（旧版这些缺陷导致插件实际上不可用）：

- **配置无法保存**：旧版在 Pages 保存时只替换内存 dict，既不调用 `AstrBotConfig.save_config()`，
  还把官方配置对象换成了普通 dict —— 重启即回滚。现在保存走 `save_config_async()` 真正落盘。
- **保存会清空未提交的配置项**：改为**深度合并**，前端只提交自己管理的字段。
- **管理员死锁**：旧版 `admin_ids` 默认为空，而唯一的授权入口又要求「已是管理员」，
  新用户装完无法自举，`/刷新模型` 永远不可用。现在改用 AstrBot 自带的 `event.is_admin()`。
- **默认 LLM 回退必然抛异常**：`llm_generate` 的 `chat_provider_id` 是必填参数，
  旧版写了个不传它的回退分支。现在显式解析 provider，并在没有 LLM 时**退化为原描述出图**。
- **子目录模型被误杀**：旧版把含 `/` `\` 的模型名判为「无效」，
  而 ComfyUI 对子目录模型返回的正是 `SDXL/xxx.safetensors`。现在按真实清单做白名单校验。
- **出图等待是无上限死循环**：旧版 `while True` 没有总超时，且「无图片输出」会永久挂转。
  现在有硬超时、按队列长度补偿、正确识别中断与执行失败。
- **半死配置项**：删除了从未被消费的 `model_switch.controlnet`；`vae` 改为显式配置优先。

新增：

- **工作流模板引擎**：模板成为可替换的数据文件，支持用户自带 API 工作流（图结构自动推导注入点）
- **架构自适应**：SD1.5 / SDXL / Pony / Flux 自动匹配分辨率、CFG、采样器、guidance
- **Flux 支持**：分离权重（`flux_unet`）与一体化底模（`flux_checkpoint`）两种模板
- **模型发现重写**：改用 `GET /models` + `GET /models/{folder}`，老版本回退到 `/object_info`（兼容两种 schema）
- **结构化错误**：解析 `node_errors` 并翻译成中文，含「可用项」提示
- **行内参数**：比例、尺寸、种子、步数、CFG、采样器、批次、LoRA、模型、负面词
- **无指令出图**（可开关，默认关闭）
- **按节点标题注入 + `bindings` 显式覆盖**：支持输入键不叫 `text` 的自定义提示词节点
- **节点能力探测**：出图前对照服务端已装节点挑模板，缺节点时明确报出缺哪个
- **配额落盘**、**图片保留策略**、**模板/状态查看指令**
- **出图失败的报错变得可定位**：带出 `error.details` 与**全部**节点级原因，
  并把 ComfyUI 的原始响应写进日志；无节点级原因时直接指出该去 ComfyUI 控制台看哪一行
- **启动横幅**：打印版本号、模板数、数据目录与日志路径，用于确认跑的是哪一版
- **修正版本声明**：原来写的 `>=4.9.2` 是错的（`astrbot.api.web` 依赖 FastAPI，
  自 v4.26.0 才有），实测后改为 `>=4.26.0`，并为两处更新版才有的 API 补了文档化回退
- **自带测试**（84 条断言）与 **API 面核验器** `tests/check_api_surface.py`

移除：

- 空转的「把几百个模型名分批喂给 LLM 生成风格标签」链路（耗时长、烧 token，产出从未被使用）
- 从未被读取的死文件 `workflows/text2img_api.json`，以及未被 import 的依赖声明
- Pages 后端那套「猜 `file_response` 不存在」的三层降级（它是官方正式 API）
- `/管理员` `/白名单` `/黑名单` 指令（改由配置项承载，且真正持久化）

**v0.2.0** — 修复新版 ComfyUI 模型列表解析；新增模型池校验
**v0.1.0** — 第一版

---

## 开发与测试

```bash
# 逻辑测试：自带 AstrBot / aiohttp 最小桩，不依赖第三方库
python tests/test_logic.py

# 插件页前端测试：用最小 DOM 桩真正执行 app.js（需要 Node）
node tests/test_pages_js.js
```

测试自带 AstrBot / aiohttp 的最小桩，**不依赖任何第三方库**即可运行，
覆盖参数解析、尺寸对齐、模型名校验、错误解码、模型发现的两条路径、模板推导与注入、
按标题注入与 `bindings` 覆盖、非法绑定拒绝、不可达节点剔除、节点能力探测与缺节点报错、
存储与配额、Pages 的合并保存语义、以及 mock ComfyUI 下的完整出图流程。

```bash
# API 面核验：对照真实 AstrBot 源码，确认插件用到的每个符号与方法都存在
python tests/check_api_surface.py /path/to/AstrBot
```

`test_pages_js.js` 是前端唯一能抓到「变量改名漏改」「渲染时抛错」的测试 ——
`node --check` 只验证语法，不检查标识符引用是否有效。它执行的是真实的
`loadStats` / `openDetail`，并把渲染出的 HTML 与弹窗字段逐一断言。

`check_api_surface.py` 用于 AstrBot 升级后复检：它不启动 AstrBot，而是直接读源码 AST，
核验 import 的符号与调用的成员（含 `Context` / `StarTools` / `AstrBotConfig` /
`AstrMessageEvent` / `Image` / Pages 请求代理与响应 helper）是否仍然存在，
避免「某个方法改名后插件静默失效」。未提供路径时以退出码 2 表示跳过。

它还会报告「当前 AstrBot 版本上哪些成员不存在、因此走了哪条回退路径」。已在
**v4.26.0**（声明的下界）、**v4.27.3**、**v4.28.1** 三个版本上跑通，均无问题：

```
$ python tests/check_api_surface.py /path/to/AstrBot-v4.26.0
[import 符号] 核验 14 项，问题 0 个
[API 成员] 核验 37 项，问题 0 个
[回退路径] 当前版本上以下成员不存在，插件会走已实现的回退：
  ~ Star 基类.logger 不存在 -> 走回退路径（缺失时 main.py 回退到 logging.getLogger("astrbot")）
  ~ AstrBotConfig.save_config_async 不存在 -> 走回退路径（缺失时 main.py 回退到 AstrBotConfig.save_config()）
```

## 👥 贡献指南

- 🌟 Star 这个项目！
- 🐛 提交 Issue 报告问题
- 🔧 提交 Pull Request 改进代码
