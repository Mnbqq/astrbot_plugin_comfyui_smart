![:name](https://count.getloli.com/@astrbot_plugin_comfyui_smart?name=astrbot_plugin_comfyui_smart&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

[![CI](https://github.com/Mnbqq/astrbot_plugin_comfyui_smart/actions/workflows/ci.yml/badge.svg)](https://github.com/Mnbqq/astrbot_plugin_comfyui_smart/actions/workflows/ci.yml)

# AstrBot 的 ComfyUI 智能绘图插件

让 AstrBot 连上你的 ComfyUI：**一句话出图、出视频**，带并发排队、实时进度、取消、多后端、换 VAE/LoRA、中英双语界面。

- **出图**：文生图 / 图生图 / **扩图** / **局部重绘**（配置页涂抹遮罩）
- **视频**：文生视频 / 图生视频，**8G 显存也能跑**（GGUF 量化 + Turbo/蒸馏模型）
- **智能**：LLM 改写提示词（中文自动转英文 tag）、看图反推、按底模架构自动匹配工作流与参数
- **模板即数据**：`workflows/*.json` 丢进去就生效，**ComfyUI 界面导出的工作流也能直接用**（自动转 API 格式）
- **多语言**：中文 / English；**多后端**：多台 ComfyUI 按负载分流 + 故障熔断

## 快速开始

1. 把插件目录放进 AstrBot 的 `data/plugins/`，重启 AstrBot；
2. WebUI → 插件 → **ComfyUI 智能绘图** → 填 ComfyUI 地址（如 `http://127.0.0.1:8188`）；
3. 聊天里发 `/画图 一只猫在草地上`。

> **强烈建议**：给 ComfyUI 加启动参数 `--cache-none`（出完图即卸载模型）。16G 内存的机器不加这个，切换大模型时容易报 `os error 1455 页面文件太小`，甚至让 ComfyUI 崩溃。

## 功能开关（默认只开文生图）

插件**按功能开放能力**：默认只有 **文生图** 可用，其余全部关闭。关闭的功能**静默忽略** —— 收到对应指令时插件**不做任何回复**，也不会占用显存（想给用户提示就自己改代码里的那两行 `if not self.feature_enabled(...)`）。

| 开关 | 对应指令 | 默认 |
|---|---|---|
| `features.t2i` 文生图 | `/画图` | ✅ **开** |
| `features.i2i` 图生图 | `/图生图`、`/改图` | ⛔ 关 |
| `features.outpaint` 扩图 | `/扩图` | ⛔ 关 |
| `features.inpaint` 局部重绘 | 配置页「局部重绘」标签页 | ⛔ 关 |
| `features.t2v` 文生视频 | `/视频` | ⛔ 关 |
| `features.i2v` 图生视频 | `/图生视频` | ⛔ 关 |
| `features.reverse_prompt` 看图反推 | `/反推` | ⛔ 关 |

- 在 **插件配置 → 功能开关**（普通配置页）或 **插件页 → 高级 → 生成功能开关** 里打开；
- **无指令出图（LLM 工具）也受这里控制**：文生图关掉就彻底没有出图能力，模型调用绘图工具时会得到同一条提示；
- `/帮助` 会列出当前已开启的功能，`/状态` 与插件页状态面板同样显示；
- 升级提示：老配置里没有这一组时按默认值处理（**只有文生图可用**），需要视频/图生图等请手动打开。

## 命令

| 命令 | 说明 |
|---|---|
| `/画图 <描述>` | 文生图 |
| `/图生图 <描述>` + 图片 | 图生图（`--denoise` 重绘幅度，可给多档一次出多张）· **默认关** |
| `/扩图 <描述>` + 图片 | 扩图（`--left/--right/--top/--bottom/--feather`）· **默认关** |
| `/视频 <描述>` | 文生视频（`--seconds/--fps/--length/--steps/--cfg`）· **默认关** |
| `/图生视频 <描述>` + 图片 | 图生视频（发两张图 = 首尾帧）· **默认关** |
| `/反推` + 图片 | 看图反推提示词；`--详细` 附带中文画面分析（构图 / 光线 / 画风 / 可疑处）· **默认关** |
| `/取消`（`/取消 全部`） | 取消自己的任务（管理员可取消全部） |
| `/状态` | ComfyUI 是否在线、队列、显存 |
| `/模型列表` `/刷新模型` `/模板列表` | 模型与模板 |
| `/统计` | 出图次数、模型使用、最近记录 |
| `/审计` | 审计日志（管理员，可选 `--user 用户ID`） |
| `/巡检` | 模型资产巡检：重名重复 / 疑似错放 / 从没用过（管理员） |
| `/体检` | 运行环境体检：启动参数 / 显存内存 / **模板依赖的节点与权重是否齐全**（管理员）· 加 `--probe` 会把预处理器权重真跑一遍 |
| `/帮助` | 用法速查 |

行内参数（可组合）：`--model 底模 --lora 名:0.8 --vae 名 --size 1024x1024 --steps 28 --cfg 7 --seed 123 --ratio 16:9`
比例可以直接裸写：`/画图 16:9 赛博朋克城市`；`--ratio 16:9`、`比例:16:9`、`比例：16:9`（全角冒号）也都认。
提示词开关（本次生效）：**`--llm` 强制让 AI 改写 / `--no-llm` 本次不改写**

> ⚠️ **值里有空格要用引号**：`--negative "lowres, bad anatomy"`。
> `--negative` 是逐 token 取值的，不加引号只会拿到第一个词（`lowres,`），
> **剩下的会跑进描述里污染正向提示词**。插件检测到这种情况会在「已收到」提示后附一句提醒；
> 也可以写成不含空格的形式 `--negative lowres,bad_anatomy`。单引号与中文引号一样有效。

## 配置面板（插件内置 Pages）

插件自带一套配置与管理面板（AstrBot 插件页 → ComfyUI 智能绘图），分 10 个标签页：
服务器 / 模型 / LLM / 出图 / 反推 / 局部重绘 / 权限 / 高级 / **状态** / 统计。

- **面板文案全部走 i18n**：`zh-CN` / `en-US` 各 348 键，切换语言时整页（含字段名与说明）一起变；测试会断言「结构性文案没有硬编码中文」「用到的键在中英目录里都存在」；
- **配置项与面板是双向校验的**：`tests/test_pages_js.js` 会断言「`FIELDS` 里每个字段都在 `index.html` 里有控件」「HTML 里没有映射不到字段的僵尸控件」，漏同步直接测试失败；
- **状态页**会显示 ComfyUI 连接、设备、**机器档位（当前判定 + 分辨率/帧数/步数上限）**、队列、并发闸门、后端与模板清单；`/状态` 聊天命令同样带档位；
- 面板里的字段：`video_machine`（机器档位）、`video_t2v_model` / `video_i2v_model`（文生/图生默认模型）、`llm_optimize_for_video`（视频提示词交给 AI）、`server_free_before_switch`（换大模型前先卸载）。

## 配置要点

| 分组 | 关键项 |
|---|---|
| `general` | 语言（auto / zh-CN / en-US） |
| **`features`** | **功能开关：默认只开文生图**，其余按需打开（见上一节） |
| `server` | ComfyUI 地址、等待上限、**换大模型前自动卸载**（默认开） |
| `queue` | 同时出图上限、单人上限、排队超时 |
| `llm_settings` | 图片提示词优化（中文→英文 tag）、**视频提示词也交给 AI 改写**（`optimize_for_video`）、看图模型 |
| `draw_settings` | 默认底模/负面/尺寸/步数、强制 LoRA/VAE、架构覆盖 |
| `hires` | 高清修复 |
| `i2i` | 图生图的 `--denoise` 默认值、输入图上限 |
| `video` | **机器档位**、**文生视频模型**、**图生视频模型**、时长/帧率/上限、是否直接发视频 |
| `output` | 是否 @ 触发人、是否附带参数、**合并成一条转发消息**、进度提示、图片保留数量与天数 |
| `permission` | 黑白名单、冷却、每日次数、**按群/用户功能白名单**、**内容过滤**、**视频独立配额**、**审计日志** |
| `backends` / `agent` | 多台 ComfyUI 分流；LLM 工具开关 |

### 视频模型按功能分开配

- `video.t2v_model` → **文生视频**默认模型（留空 = 自动挑第一个视频权重）
- `video.i2v_model` → **图生视频**默认模型（Wan 2.2 TI2V-5B 两者共用，可填同一个）
- 聊天里 `--model xxx` 优先级最高。

## ControlNet 与放大

两个默认关闭的新能力（在「高级 → 生成功能开关」里打开）：

### ControlNet 深度控制（`--control depth`）

```
/画图 一个女孩站着，手插口袋 --control depth          ← 参考图决定构图与姿势
/画图 … --control depth --control-strength 0.6        ← 约束弱一点（默认 0.8）
/画图 … --control depth --control-end 0.5             ← 更早放手（默认 0.7）
/画图 … --control depth --control-model 别的深度模型.safetensors
```

- 工作流：参考图 → `DepthAnythingV2Preprocessor` → `ControlNetApplyAdvanced` → 采样；
- 需要的模型：`models/controlnet/control-depth-sdxl-small-fp16.safetensors`（305MB，已装；也可换 2.3GB 的完整版获得更好质量）与
  `custom_nodes/comfyui_controlnet_aux/ckpts/depth-anything/Depth-Anything-V2-Small/depth_anything_v2_vits.pth`（95MB，已装）；
- **必须配 SDXL 系底模**：插件会在没指定 `--model` 时自动挑一个 SDXL 系底模（并跳过 `inpaint`/`instruct`/`inkBase` 这类通道不匹配的变体）；
- ⚠️ 如果你原来那个 `controlnetxlCNXL_bdsqlszDepth.safetensors` 还在，它是 **ControlNet-LLLite** 格式，标准 `ControlNetLoader` 会报
  `controlnet file is invalid` —— 用上面新下的标准 ControlNet，或删掉它。
- 🛟 **深度预处理器跑不起来时会自动降级**：预处理器的权重不在 `models/` 下，是自定义节点**第一次用到时从 HuggingFace 下载**的，
  所以「节点在、`/体检` 全绿、一跑就炸」是真会发生的（`LocalEntryNotFoundError`，而 DepthAnythingV2 的默认档是 1.3G 的 `vitl`，
  多数机器只装了 95M 的 `vits`）。插件现在会在提交前用一张 **64×64 空白图把预处理器单独跑一遍**：
  探得通才走 ControlNet，探不通就**自动改用图生图保构图**（`--denoise 0.45`，你显式写过 `--denoise` 就按你的来），
  并在消息里说清原因。这一探只在首次使用时花一次额外往返，结果按（后端 + 节点 + 权重名）缓存 10 分钟。
  探测最多等 60 秒：**连不上 HuggingFace 时报错要等退避重试**（实测单发一次约 42 秒，连着发多个最久见过 3.5 分钟），
  而权重正常时 64×64 只要几秒；如果超时那一刻任务**还在排队**（服务器忙），算「没结论」照常出图，不会误降级。

### 放大（`/放大` 与 `--upscale`）

```
/放大 --scale 2                        ← 把图放大 2 倍（默认 2，最大 4）
/放大 --scale-to 1920x1088             ← 放大到精确尺寸（壁纸/头像要的是固定宽高）
/画图 一只猫 --upscale 2               ← 出图后自动放大一遍
/画图 一只猫 --upscale 1920x1088       ← 出图后直接放大到指定尺寸
```

- 工作流：`UpscaleModelLoader` → `ImageUpscaleWithModel`（4x 模型）→ `ImageScaleBy` 缩回目标倍数；
  `--scale-to` 走 `upscale_exact` 模板，最后一步换成 `ImageScale`（Lanczos）落到精确宽高；
- **什么时候用 `--scale-to`**：`--scale` 只保证「倍数」，4x 再乘 0.5 未必落回你要的尺寸
  （1920×1088 这种非 4 的整数倍尤其对不上）；要固定尺寸就用 `--scale-to`；
- 模型：`models/upscale_models/4x-UltraSharp.safetensors`（64MB，已装）。

### 图生图的重绘幅度（`--denoise`）

`denoise` 是图生图里唯一一个「说不清该给多少」的参数，实测口径：

| 幅度 | 效果 |
|---|---|
| `0.30 ~ 0.40` | 几乎只换细节、修脸修手，**原图色调与构图基本不动** |
| `0.40 ~ 0.50` | 保构图保色调地重绘（ControlNet 降级时用的就是 **0.45**） |
| `0.55 ~ 0.65` | 开始换画风、换服装材质，主体还在 |
| `0.70 ~ 1.00` | 接近重画，只借个构图 |

拿不准就**一次给多档**，串行出图直接对比：

```
/图生图 改成夜景 --denoise 0.4,0.55,0.7     ← 升序去重，最多 4 档
```

多档对比会复用第一档算出来的提示词，不会多花 LLM 调用；档数直接乘等待时间，别贪多。

## 治理与可控性

| 能力 | 配置项 | 说明 |
|---|---|---|
| **按群/用户功能白名单** | `permission.feature_rules` | 一行一条：`user:123456=t2i,i2v` / `group:987654=t2i` / `default=t2i`；功能名同功能开关，可写 `all` / `none`。优先级 **user > group > default > 全局功能开关**；管理员在「豁免」打开时不受限 |
| **内容过滤** | `permission.nsfw_filter` + `nsfw_words` + `nsfw_negative` | 命中词表的描述直接拦下（**默认静默**，可开 `nsfw_notify` 提示）；用户原话与 AI 改写后的词都检查；开启时可附加负面词 |
| **视频独立配额** | `permission.video_daily_limit` / `video_cooldown` | 视频一条动辄几分钟，单独限次与冷却，不与出图同权 |
| **审计日志** | `permission.audit_log`（默认开） | 记录每次生成（用户/群/用途/模型/耗时/提示词前 60 字）与内容过滤拦截；管理员 `/审计 [条数] [--user ID]` 查看，页 API `/audit`，落盘 `audit.jsonl`（保留最近 2000 条） |

## 机器档位与推荐模型

`video.machine` 决定**分辨率 / 帧数 / 步数上限**，防止低配机器被打爆。
**低配 / 标配档下，没指定模型时会优先挑「加速版」权重**（文件名带 `turbo` / `lightning` / `distill` / `schnell` 等）——实测 Turbo 4 步 74 秒、原版 20 步 281 秒。想固定用哪个就填 `video.t2v_model` / `i2v_model`，或聊天里 `--model`。填 `auto`（默认）按显存自动判定：≤10G → low，12~20G → mid，≥24G → high；**内存不足 20G 再降一档**；探测不到就按最低档保守处理。

| 档位 | 硬件标准 | 视频上限 | 推荐视频模型 | 推荐图片模型 |
|---|---|---|---|---|
| **低配** | 显存 ≤10G **或** 内存 ≤16G | 480p / 97 帧 / 12 步 | Wan 2.2 TI2V-5B **Q4_K_M GGUF**（质量）· **Turbo Q4 GGUF**（快 3.8 倍）· LTXV 2B GGUF（最省）· Wan 2.1 T2V 1.3B | SD1.5 系（最快）· Animagine XL 4.0 / RealVisXL V5（SDXL，1024 只要 31 秒） |
| **标配** | 显存 12~20G **且** 内存 32G | 720p / 121 帧 / 30 步 | Wan 2.2 TI2V-5B **fp16**（单模型 9.3GB）· 同款 Q8 GGUF | 上面全部 · Flux.1 schnell GGUF（4 步） |
| **高配** | 显存 ≥24G **且** 内存 ≥64G | 720p / 161 帧 / 50 步 | 同上 fp16 + 更长片段（14B 是双模型两段式，需另加模板） | 上面全部 · Flux dev fp16 · Qwen-Image（建议 ≥32G 内存，**工作流模板要自带**，见下） |

**实测参考**（8G 显存 / 16G 内存，即低配档）：

| 任务 | 模型 / 参数 | 耗时 |
|---|---|---|
| 文生图 512x768 | SD1.5 | 9 秒 |
| 文生图 1024x1024 | Animagine XL 4.0（28 步） | 31 秒 |
| 文生图 1024x1024 | Flux.1 schnell GGUF（**4 步**） | 35 秒 |
| 文生视频 832x480 / 81 帧 | Wan 2.2 TI2V-5B **Turbo**（**4 步**） | 74 秒 |
| 文生视频 832x480 / 81 帧 | Wan 2.2 原版（20 步） | 281 秒 |
| 文生视频 768x512 / 97 帧 | LTXV 2B GGUF（8 步） | 34 秒（画质待修，暂别用） |

**模型放哪**：底模 → `models/checkpoints/`；视频 unet（含 GGUF）→ `models/diffusion_models/`；文本编码器 → `models/text_encoders/`；VAE → `models/vae/`；LoRA → `models/loras/`。
> GGUF 的目录映射各安装不同：打开 `http://<你的ComfyUI>/experiment/models` 看真实路径（有机器把 `unet_gguf` 映射到 `diffusion_models`）。插件**按 `.gguf` 扩展名**识别，放哪个被注册的目录都能认。

## 合并转发消息

出多张图时（Hires / 多档 denoise / 一次出多张）会连着发好几条，群里比较吵。
打开 **配置页 → 结果发送与图片保留 → 合并成一条转发消息**（`output.merge_forward`，
默认关），参数文字与图片就会打包成**一条**「合并转发」消息：

```
┌─ 合并转发 ────────────────┐
│ ComfyUI 智能绘图           │
│ 🖼 sd_checkpoint（sdxl）… │
│ [图片] [图片] [图片]        │
└────────────────────────────┘
```

- **只有 QQ 个人号 / OneBot v11（aiocqhttp）支持**，这是 AstrBot 用 `Node` 组件发
  「群合并转发消息」的能力，其它平台发了会直接报错；
- 转发卡片上的昵称由 `output.merge_forward_name` 控制，留空用「ComfyUI 智能绘图」，
  卡片里的 uin 用**机器人自身 id**（`get_self_id()`）；群聊里的 @ 留在卡片**外面**
  （塞进卡片里就提醒不到触发人了）；
- **三种情况会自动改回普通发送**（宁可拆开发，也不要把结果整条丢光）：
  1. 平台不是 `aiocqhttp`；
  2. 结果是视频 —— AstrBot 的 `Node.to_dict()` 只对图片/语音做 base64 内嵌，视频走的是
     同步 `toDict()`，把 `file://` URI 原样塞进节点，适配器不保证能解析；
  3. 图片总量超过 **20MB** —— 图片要转成 base64 内嵌（体积 +33%），一条几十 MB 的消息
     很可能发送失败。
- 另外注意：**base64 内嵌是有代价的**。一张 1920×1088 的 PNG 约 3MB，内嵌后约 4MB；
  四张就是 16MB。开了合并转发又常出大图的话，可以在
  `output` 里把图片保留格式调小，或者干脆别开。

## AI 改写提示词（图片 / 视频两套）

| 用途 | 改写目标 | 开关 | 说明 |
|---|---|---|---|
| 图片 | 英文 tag 堆叠 + 选底模/LoRA/VAE（选中 LLM 编码器底模时改走通顺句子，见下） | `llm_settings.enable_prompt_optimize` | 中文提示词在 SD1.5/SDXL 上基本无效，靠它翻译 |
| **视频** | **动作 + 镜头 + 光影**（保持你的语言） | `llm_settings.optimize_for_video` | 视频要的是「她缓缓回头、镜头推近」，不是 tag；视频模型由 `video.t2v_model` / `i2v_model` 决定，**LLM 只改提示词、不选模型** |
| 反推 | 看图输出英文 tag（`/反推`；加 `--详细` 另附中文画面分析） | `features.reverse_prompt` | 可用 `vision_settings` 单独指定看图模型 |

聊天里可以临时覆盖：`--llm` 强制改写、`--no-llm` 本次不改写（对图片和视频都生效）。

### 给 LLM 的约束（v0.27.0 重整）

提示词模板在 `.astrbot-plugin/i18n/*.json` 的 `llm.*` 键里，中英各一份；**中文那份必须与
`llm_service.py` 的常量逐字节一致**（有测试兜着，防止两边漂移）。改这四条是因为实测出图质量
受它们影响最大：

- **防脑补**：明确要求「只画用户说过的内容，角色/服装/道具/场景都不要新增」。此前没有这条，
  LLM 会把「一个女孩在看书」扩写成带樱花、夕阳、风吹裙摆的另一张图。
- **不许重复插件本地会补的东西**：
  - 通用质量词（`masterpiece` / `best quality` / `8k` / `ultra detailed`）由**架构档案自动补**；
  - 通用手部与肢体内脏词由 **`ANATOMY_NEGATIVE` 自动补**。
  此前模板要求 LLM「必须列举」那一长串手部负面词，合并去重后一点增益都没有，纯烧 token。
  现在只让 LLM 写**与这次描述相关**的缺陷词，想不出就留空。
- **给数量区间**：图片 15~30 个 tag、反推 15~45 个。tag 堆太多会稀释每个词的权重
  （插件自己都会为此提示用户）。
- **选型有据可依**：模板里给 checkpoint 标的是**架构**（sdxl / pony / flux），看不出写实还是动漫，
  所以提示词里点明「模型名自带线索」——`realvis` / `realistic` / `photo` / `juggernaut` 偏写实，
  `animagine` / `anything` / `anime` / `pony` / `illustrious` 偏动漫，不确定就选清单里第一个。

### 提示词结构：七段（v0.32.0）

不管最终写成 tag 还是句子，提示词都按这七段组织 —— **每段都要尽量覆盖，但只写真有的**，
写不出就跳过（宁可少写，也不许为了凑满七段编内容）：

| 段 | 内容 | 谁来写 |
|---|---|---|
| 1 主体 | 一人还是多人、什么类型（`1girl, solo` / `no humans`） | LLM |
| 2 场景与环境 | 在哪、什么环境与背景 | LLM |
| 3 动作与状态 | 在做什么、什么姿势、**手在干什么** | LLM |
| 4 细节特征 | 材质、纹理、颜色 | LLM |
| 5 光照与氛围 | 光源方向、色温、氛围 | LLM |
| 6 风格 | 艺术风格、摄影／媒介 | LLM |
| 7 质量修饰词 | `masterpiece` / `score_9` / `8k` 这类 | **默认插件按架构补**，可切给 LLM |

- **tags 模式的实际顺序**（`add_quality_tags` 打开时）：主体 → 外貌 → 服装配饰 → **细节特征**
  → 动作与手部 → 视角构图 → 场景背景 → 光线氛围 → 画风。这是 CLIP 的**权重顺序**
  （越靠前影响越大），所以「场景」排在后面而不是第二段 —— 提到前面会稀释主体。
- **natural 模式的顺序**（Qwen-Image 一类）：主体 → 场景 → 动作 → 细节 → 光照 → 风格，
  写进 1~3 句通顺英文。
- **第 7 段由谁写**由 `draw_settings.quality_words_by` 决定：
  - `plugin`（默认）：LLM 不写，插件按底模架构补（SD1.5 补 `masterpiece, best quality`、
    Pony 补 `score_9…`、SDXL / Flux 不补）。
  - `llm`：同时在用户消息里允许 AI 写 2~4 个**与本次画面相关**的（如 `film grain`、`soft bokeh`）；
    插件补的通用词仍会合并，**重复的自动去掉**。想完全交给 AI 写，把上面的「按架构自动补质量词」
    一起关掉即可。
  - 两种模式**共用同一套系统提示词**，只换用户消息里那一行 —— 免得出现两份会各自漂移的模板。
- 为什么默认不让 LLM 写质量词：通用质量词由插件按架构补一次就够，让 LLM 再写一遍纯烧 token
  （v0.27.0 的结论）；但有些画面确实需要针对性的质感词，所以留了这个开关。

### 自然语言编码器底模（Qwen-Image 一类，v0.31.0）

SD1.5 / SDXL / Pony 的文本编码器是 CLIP，喂英文 tag 最准；而 **Qwen-Image 这一类的编码器是 LLM**，
它吃的是通顺的句子 —— 堆 tag、补 `masterpiece`、塞 `((强调))` 都是白费。所以插件让 LLM 在
**选底模的同一次调用**里就把这次的写法一并声明出来（`prompt_style`）：

| prompt_style | 什么时候 | 插件随后做什么 |
|---|---|---|
| `tags`（默认） | 其余底模 | 照旧补架构质量词 / Pony 分数前缀 |
| `natural` | 选中的底模名字含 `qwen-image` | **不补**质量词与分数前缀，正向就是那几句通顺英文 |

- **为什么由 LLM 声明**：改写那一刻插件还不知道会选中哪个底模（底模是 LLM 自己从清单里挑的），
  所以只能让选型那次调用一并决定；事后插件再用底模名**交叉校验**。
- **两个方向都校验、都不静默**：底模看上去是自然语言编码器却给了 tag，结果里附 ⚠️ 提示；
  反过来模型自称 `natural`、但底模名**明确命中** CLIP 系家族（SD1.5 / SDXL / Pony…）时也提示，
  并**不硬把** `masterpiece` 塞进散文里（那种混合体更难排查）。名字谁都不认识时按模型说的走 ——
  那可能正是新出的 LLM 编码器底模，不能误报。
- **负面词不跟着关**：要不要下发负面词是「采样器用不用 CFG」的问题（只有 Flux 那种 CFG=1 的才关），
  与提示词写成句子还是 tag 无关 —— 所以 `natural` 下 `bad hands` 那类安全网照旧带。
- 顺带说明两条**不属于**模板能力、写进提示词也没用的事：`((加权))` LLM 编码器不解析；
  `8K / 1920x1080` 也**改不了画幅**（画幅由 latent 尺寸决定）。
- ⚠️ **插件目前没有 Qwen-Image 的工作流模板**：这一条只解决「提示词怎么写」。
  Qwen-Image 的编码器 / VAE / 模型加载跟 SD、Flux 那套都不一样，想出图得自己往 `workflows/`
  放一个 API 格式模板（把模板给我，我可以按现有规范并进去）。

### 反推的三条硬要求

反推最容易出三类问题，都在模板里明确禁止了：

1. **把签名/水印当内容**：原图角标写着画师名，不禁止的话会被推成 tag。
2. **真人照片推成二次元**：照片被推成 `1girl, anime style` 之后，拿去出图就变画风了。
   模板要求先判断画面类型，照片走 `photo / realistic / 35mm photograph` + `woman / man / person`。
3. **编造看不清的细节**：宁可少写 —— 编出来的 tag 会让重绘结果偏离原图。

### 详细分析模式（`/反推 --详细`，v0.30.0）

想看「这张图画得怎么样、有没有 AI 痕迹」时加 `--详细`（也可写 `--detail` / `--分析`）：
英文 tag 照旧，另外回一份中文分区分析。

| 分区 | 看什么 |
|---|---|
| `composition` | 构图与取景：主体位置、视线引导、前中后景、留白 |
| `lighting` | 光线与色彩：主光方向、冷暖对比、光源类型、整体色调 |
| `style` | 画风与笔触：媒介、完成度、风格线索（**明令不许断言具体模型或画师**） |
| `anomalies` | **可疑处**：手指 / 四肢等结构崩坏、透视或镜像漂移、画面上的伪文字与水印；每条一句话、最多 5 条，**没有就留空数组** |

- **为什么要分区**：分区是**可核对的** —— 模型没法用一句「整体氛围很好」糊过去。
- **为什么允许空数组**：这类需求最容易让模型硬凑问题，而一条编出来的「问题」会直接误导你。
- 分区缺失时**整行不显示**（不会留下一个空的「构图：」）；模型把整段分析写成一段话也能正常显示。
- `--详细` 是**纯开关**：`/反推 --详细 帮我看构图` 里的「帮我看构图」仍作为额外要求传给模型。
- 与 `--画` 可叠加：`/反推 --详细 --画` 会按反推出的 tag 直接出图。

## 中文提示词

| 用途 | 中文 | 说明 |
|---|---|---|
| 视频（Wan / LTXV） | ✅ 直接写 | 一定要写**动作**：「她缓缓回头、长发飘动、镜头缓推」 |
| 图片（SD1.5 / SDXL） | ❌ 基本无效 | 打开 **LLM 提示词优化**（自动转英文 tag），或直接写英文 |
| 图片（Flux） | ⚠️ 能出图但丢细节 | 建议英文 |

## 模板（16 个，放 `workflows/` 即生效）

| 文件 | 用途 |
|---|---|
| `sd_checkpoint` · `img2img_checkpoint` · `outpaint_checkpoint` · `inpaint_checkpoint` | 文生图 / 图生图 / 扩图 / 局部重绘 |
| `flux_checkpoint` · `flux_unet` · `flux_schnell_gguf` | Flux：一体化 / 分离权重 / GGUF 4 步 |
| `lumina_checkpoint` | Lumina-Image-2.0（1024 / 30 步） |
| `controlnet_sdxl` | ControlNet 深度控制（`--control depth`） |
| `upscale` · `upscale_exact` | 放大：按倍数（`--scale 2`）/ 精确宽高（`--scale-to 1920x1088`） |
| `wan_t2v` · `wan_i2v` | Wan 2.x（safetensors）文生 / 图生视频 |
| `wan22_t2v_gguf` · `wan22_i2v_gguf` | Wan 2.2 TI2V-5B（GGUF，低配首选） |
| `ltxv_t2v` | LTX-Video 2B（GGUF，最省显存） |

## 巡检与体检

管理员可用两条指令自检，配置页「状态 → 诊断报告」也有同样内容（页 API `/diagnose`）：

| 指令 | 检查内容 |
|---|---|
| `/巡检` | 模型资产：**同名重复**（跨目录同模型）、**疑似错放**（VAE/文本编码器/ControlNet/放大模型混在 `checkpoints`、GGUF 丢在 checkpoints）、**从没用过**的底模（结合统计），同机时还会按体积列出最大的几个 |
| `/体检` | 运行环境：离线与否、**启动参数**（16G 内存没加 `--cache-none` 会提醒）、显存/内存余量、机器档位与视频时长的冲突、模板数量，以及**模板依赖自检** —— 模板用到的节点是否还在、硬编码的权重是否真的存在于对应目录（升级 ComfyUI 后缺节点、模板写着 fp8 的 umt5 而本地只有 GGUF 量化版，这类问题会被提前发现） |
| `/体检 --probe` | 在上一行的基础上，把**按需下载权重的预处理器**（DepthAnything 等）用 64×64 空白图**真跑一遍**。这是常规体检的盲区：`/object_info` 的下拉列表是节点源码写死的候选名，**不代表服务器上真有这个文件** |

体检把「缺失的模板默认权重」记为 warn（用 `--model`/`--vae` 覆盖即可），把「缺节点」记为 error（模板彻底不可用）；
对「按需下载的预处理器权重」只记 **info**（清单里根本查不到），要确认请用 `--probe`。

## 排错

| 现象 | 处理 |
|---|---|
| `os error 1455 页面文件太小` / ComfyUI 崩溃 | 提交内存不足：**加大 Windows 页面文件**（建议初始 16G / 最大 64G），并给 ComfyUI 加 `--cache-none`；插件也会在换大模型前先 `/free` |
| 视频看起来像静止图 | 帧数/帧率不够（Wan 要 24fps、81 帧起），或提示词只描述了画面没写动作 |
| 聊天里视频显示 0 秒 | v0.15.5 起已改输出 **mp4（h264）**；若你自带模板还写着 `SaveWEBM`，换成 `CreateVideo` + `SaveVideo` |
| `/视频` 报 `unet_name 取值 xxx 不存在` | 权重不在 ComfyUI 注册目录，或装载器用错；看 `/experiment/models` 与 `/object_info/UnetLoaderGGUF` |
| 报「没有可用的文生视频模板」 | 该权重不是视频模型，或没装对应模板；先 `/刷新模型` 再看 `/模板列表` |
| 出图中途 ComfyUI 挂了 | v0.15.6 起快速报错（连不上 60 秒 / 任务消失 6 轮），不会白等半小时 |
| 中文提示词没效果 | 图片侧开 LLM 优化（自动翻译），视频侧可直接用中文 |
| `--control depth` 报 `LocalEntryNotFoundError` / 连不上 huggingface.co | 深度预处理器权重没下载（它们不在 `models/` 里）。v0.26.0 起插件会**提前探测并自动降级为图生图**；要恢复 ControlNet，请在 ComfyUI 那台机器上让它联网跑一次，或把 `depth_anything_v2_vits.pth`（95M，**别用默认的 1.3G `vitl`**）放进 `custom_nodes/comfyui_controlnet_aux/ckpts/` 后重启，再用 `/体检 --probe` 确认 |
| 多人同时用越来越慢 | 调 `queue.max_concurrent` / `per_user_limit`；ComfyUI 本身是串行执行，并发主要买的是公平与队列秩序 |

## 更新日志（最近）

- **v0.32.0** — 提示词改为按**七段结构**组织（主体 / 场景 / 动作 / **细节特征** / 光照 / 风格 / 质量词）：七段都要尽量覆盖、但只写真有的（不许为凑满段数编内容）；补上了此前缺失的「细节特征（材质/纹理/颜色）」独立维度；natural 模式按七段的自然顺序写句子，tags 模式保留 CLIP 的权重顺序。新增开关 **`draw_settings.quality_words_by`**（默认 `plugin` = 插件按架构补；切 `llm` 则允许 AI 也写 2~4 个针对性质量词，与插件补的自动去重）。两套模式共用同一套系统提示词、只换用户消息里的一行，避免模板漂移
- **v0.31.0** — 支持**自然语言编码器底模（Qwen-Image 一类）**：让它走**通顺句子**而不是逗号 tag。LLM 在选底模的同一次调用里声明 `prompt_style`（`natural` / `tags`），`natural` 时插件**不再补** CLIP 时代的质量词与 Pony 分数前缀，但**负面词安全网照旧**（要不要下发负面词是 CFG 的事，跟提示词风格无关）；底模与风格不一致时结果里给 ⚠️ 提示，不静默。顺带把提示词长度守卫改成**按语言给预算**（英文同义表达天然长 ~2 倍，共用一把尺子只会逼人砍英文）。注意：**插件没有 Qwen-Image 工作流模板**，想出图需自带模板
- **v0.30.0** — 新增 **`/反推 --详细`**（别名 `--detail` / `--分析`）：除英文 tag 外再给一份**分区**中文画面分析 —— 构图 / 光线与色彩 / 画风 / **可疑处**（结构崩坏、透视漂移、画面上的伪文字与水印）。分区是**可核对**的，模型没法用一句「氛围很好」糊过去；可疑处**没有就留空数组**，提示词禁止为了凑数编问题。两套提示词共用同一段 tag 规则（不复制，避免「普通模式禁了、详细模式没禁」的漂移），`--详细` 是纯开关所以后面的文字仍算额外要求，可与 `--画` 叠加
- **v0.29.0** — 修三处**静默失效**的行内参数：`--negative` 值里有空格会被截断（剩下的词跑进描述**污染正向提示词**），现在支持**半角/单双引号与中文引号**（`--negative "lowres, bad anatomy"`），并在检测到截断时于「已收到」提示后附一句提醒；**裸比例**（`/画图 16:9 …`，设计文档里的示例写法）此前完全不生效，现在能识别；**全角冒号** `比例：16:9` 也认了
- **v0.28.0** — 新增**合并转发消息**（`output.merge_forward`，默认关）：把参数文字与图片打包成一条 AstrBot 的「群合并转发」消息发送，出多张图时不再刷屏；卡片昵称可配（`merge_forward_name`），uin 用机器人自身 id，群聊 @ 留在卡片外；**平台不是 aiocqhttp、结果是视频、图片总量超 20MB 这三种情况自动改回普通发送**
- **v0.27.0** — **重整 LLM 提示词**（生图 / 视频 / 反推三套）：生图加上**防脑补**约束（只画用户说过的内容）与 tag 数量区间（15~30），不再让 LLM 重复插件本地会补的通用质量词与手部负面词（纯烧 token），选型规则明确「按模型名线索挑写实/动漫底模」；反推明确**忽略签名水印**、**区分真人照片与插画**（照片不再被推成 `1girl, anime`）、tag 数量 15~45；中文文案与代码常量的一致性、以及上述每条规则都有断言兜着
- **v0.26.0** — **修掉「节点在、体检全绿、一跑就炸」这类盲区**：ControlNet 的深度预处理器权重是自定义节点按需从 HuggingFace 下载的（不在 `models/` 里），现在提交前会用 64×64 空白图**探测**，不通就**自动降级为图生图保构图**（denoise 0.45）而不是甩一段英文报错；新增 `/体检 --probe` 真跑一遍预处理器、依赖自检把这类权重单列为 info；放大新增 **`--scale-to 1920x1088`** 精确尺寸（`upscale_exact` 模板）；`--denoise` 支持**多档一次出多张**（`--denoise 0.4,0.55,0.7`）
- **v0.25.0** — 新增 **ControlNet 深度控制**（`/画图 … --control depth` + 参考图，自动偏好 SDXL 底模）与**放大**（`/放大 --scale 2`、出图加 `--upscale 2` 自动放大）；后处理类模板支持 `prompt_required: false`
- **v0.24.0** — 配置面板**文案全量键化 + 中英双语**（185 条：标签/标题/说明/选项/占位符），页语言切换现在整页生效；新增 `data-i18n-html` / `data-i18n-placeholder` 支持与 3 条防回归断言（硬编码中文 / 键缺失 / 中英键集合一致）
- **v0.23.0** — 工程化：**GitHub Actions CI**（Python 3.10/3.11/3.12 跑逻辑与前端测试 + 上架形态检查 + AstrBot API 面核对）、**tag 自动发布 Release**（附源码包）；新增 `/巡检`（模型资产）与 `/体检`（环境 + **模板依赖自检**）与页 API `/diagnose`
- **v0.22.0** — 治理三件套：按群/用户**功能白名单**（user > group > default > 全局）、**内容过滤**（默认静默拦截，可附加负面词）、**视频独立配额**（单独限次与冷却）、**审计日志**（`/审计` + `/audit` API，所有生成入口统一记账）
- **v0.21.1** — 关闭的功能**静默忽略**（此前会回「功能未开启」），无指令出图同样静默；提交署名统一为 `Mnbqq`
- **v0.21.0** — 新增 7 个**功能开关**（`features.*`）：默认**只开文生图**，图生图 / 扩图 / 局部重绘 / 文生视频 / 图生视频 / 反推全部默认关闭；关闭时指令直接提示去哪打开、不占显存，无指令出图（LLM 工具）同样受控；`/帮助`、`/状态`、状态面板都会列出已开启功能
- **v0.20.0** — 配置面板同步：补上漏掉的 5 个控件（机器档位 / 文生·图生默认模型 / 视频提示词 AI 开关 / 换大模型前先卸载）；状态页与 `/状态` 显示机器档位与各项上限；新增「FIELDS ↔ 面板控件」双向一致性测试
- **v0.19.0** — 低配/标配档默认**优先挑加速版权重**（turbo/distill/schnell…），实测省 3~4 倍时间；配置或 `--model` 仍可覆盖
- **v0.18.1** — Wan safetensors 模板改用 **GGUF 文本编码器**（umt5 Q4 仅 3.4GB，原 fp8 要 6.3GB）+ `wan_2.1_vae`；实测 Wan 1.3B 在本机**比 5B 还慢**（391s vs 281s），已在推荐表里标注不推荐
- **v0.18.0** — LLM 参与**视频**提示词：新增视频专用改写（补动作/镜头/光影、保持中文）+ 开关 `llm_settings.optimize_for_video`；新增行内开关 `--llm` / `--no-llm`（图片与视频都能临时覆盖）
- **v0.17.0** — 视频模型可按功能分别配置（`t2v_model` / `i2v_model`）+ 机器档位（`machine`：auto/low/mid/high，自动限制分辨率/帧数/步数）；README 瘦身，工程细节移到 `docs/`
- **v0.16.0** — 支持 LTX-Video 2B（GGUF，最省显存的一档）
- **v0.15.9** — 换大模型前自动 `/free` 卸载，修 `os error 1455` 崩溃
- **v0.15.8** — 修「指定 VAE 只改解码侧」；新增 Flux schnell GGUF / Lumina 2 模板
- **v0.15.5** — 视频输出改成 **mp4（h264）**，修聊天端「0 秒」
- **v0.15.4** — 视频节奏改成模型原生（Wan 24fps / 81 帧）

完整历史见 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)。

## 文档

- [`docs/机器档位与模型.md`](docs/机器档位与模型.md) — 三档硬件标准、每个功能的模型清单与下载建议
- [`docs/内部设计.md`](docs/内部设计.md) — 模板机制（bindings / params）、校验与能力探测、界面格式转换、多语言、队列治理、历次真机实测
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — 完整更新日志

## 开发

```bash
python3 tests/test_logic.py      # 逻辑 + mock ComfyUI 全链路
node   tests/test_pages_js.js    # 配置页前端
python3 tests/check_api_surface.py <AstrBot 源码目录>   # 上架 API 面核对
```

### 发版

tag 消息沿用 `ComfyUI 智能绘图 vX.Y.Z` + 空行 + 一句话说明的格式：

```bash
git tag -a v0.29.0 -m "ComfyUI 智能绘图 v0.29.0" -m "一句话说明这一版改了什么"
git push origin refs/tags/v0.29.0      # ← 一次只推一个 tag
```

推上去后 `.github/workflows/release.yml` 会打包 `astrbot_plugin_comfyui_smart_<tag>.tar.gz` 并创建 Release。
推 `main` 只跑 CI，不会自动发版。

> ⚠️ **一次只能推一个 tag**。把多个 tag 塞进同一条 `git push`
> （`git push origin v0.26.0 v0.27.0 …`）**不会触发任何工作流** —— 这是 GitHub Actions 的已知问题
> （[actions/runner#3644](https://github.com/actions/runner/issues/3644)），
> 表现为 tag 明明推上去了、仓库里也有，但既不跑 Release 也不报错，很容易误以为是权限问题。
>
> 已经一起推上去的补救办法是**先删再逐个推**（delete 事件不触发工作流，所以删的时候可以一次删多个）：
>
> ```bash
> git push origin --delete refs/tags/v0.26.0 refs/tags/v0.27.0
> git push origin refs/tags/v0.26.0
> git push origin refs/tags/v0.27.0
> ```

#### 重建已有 Release 的正文

Release 正文由 `docs/CHANGELOG.md` 里对应那一节生成（见 `.github/scripts/release_notes.py`）。
如果某个 Release 是在这套流程之前发的，正文只会有一行 compare 链接。

**重推 tag 是没用的** —— tag 推送用的是「该 tag 提交里」的 workflow，旧 tag 里那份
`release.yml` 还是老的（已实测确认：重推 v0.25.0 后正文一个字都没变）。
正确做法是手动触发：

**Actions → Release → Run workflow** → `tag` 填版本号（如 `v0.25.0`）→ Run。
它会检出 `main`、用最新的脚本与 CHANGELOG 生成正文，并按该 tag 的内容重打源码包；
**同名的 Release 会原地更新**，不会重复创建。

触发页面：https://github.com/Mnbqq/astrbot_plugin_comfyui_smart/actions/workflows/release.yml

## 许可

MIT
