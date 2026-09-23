![:name](https://count.getloli.com/@astrbot_plugin_comfyui_smart?name=astrbot_plugin_comfyui_smart&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# AstrBot 的 ComfyUI 智能绘图插件

让 AstrBot 连接 ComfyUI，用一句自然语言出图：

> **/画图 一个白裙少女站在樱花树下**

插件读取你 ComfyUI 里**真实存在的模型**，识别底模架构（SD1.5 / SDXL / Pony / Flux），
自动匹配工作流模板、分辨率、CFG 与采样器，再用 LLM 把中文改写成提示词。

- **模板是数据**：`workflows/*.json` 是真正被加载的文件，可以直接换成你自己的 API 格式工作流
- **不靠猜**：模型、节点、参数都对着你 ComfyUI 的真实清单校验，缺什么、错在哪都直接说清楚
- 另有 Hires Fix、图生图、看图反推提示词、无指令出图、统计与画廊、权限配额

---

## 快速开始

1. 把插件目录放进 AstrBot 的 `data/plugins/`，重启 AstrBot
2. 打开插件配置页，填写 ComfyUI 地址（如 `http://127.0.0.1:8188`）
3. 发 `/画图 你的画面描述`

**环境要求**：AstrBot >= 4.26.0、Python 3.10+、一个可访问的 ComfyUI。

下界不是拍脑袋写的，是 `tests/check_api_surface.py` 对着真实源码实测出来的：

| 用到的 API | 自哪个版本起提供 | 更早版本的行为 |
|---|---|---|
| `astrbot.api.web`（`json_response` / `file_response` / `request`） | v4.26.0 | **无回退**，这是硬下界 |
| `AstrBotConfig.save_config_async` | v4.27.0 | 自动回退到同步的 `save_config()` |

已在 **v4.26.0**、**v4.27.3**、**v4.28.1** 三个版本上跑通，均无问题。

> **不需要 LLM 也能用**：没配置对话模型时，插件直接拿你的原话当提示词、用第一个可用底模出图，
> 并在结果里注明。只有配了 LLM 才会做中文改写与模型选型。

---

## 指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/画图 <描述>`（别名 `/绘图` `/draw` `/生成图片`） | 生成图片；带图发送或回复图片会自动走图生图 | 按配置 |
| `/图生图 <描述>`（别名 `/改图` `/i2i`） | 以你发的图为底按描述重绘 | 按配置 |
| `/反推`（别名 `/识图` `/img2prompt`） | 看图反推提示词，`--画` 可直接按结果出图 | 按配置 |
| `/模型列表`（别名 `/模型`） | 查看 ComfyUI 里的模型清单 | 所有人 |
| `/模板列表`（别名 `/工作流`） | 查看已加载的工作流模板与自定义目录 | 所有人 |
| `/状态` | ComfyUI 连接、设备、队列、看图模型是否可用 | 所有人 |
| `/统计` | 出图统计与画廊 | 所有人 |
| `/刷新模型` | 重新读取模型清单 | **管理员** |
| `/帮助`（别名 `/comfy帮助`） | 显示帮助 | 所有人 |

### 行内参数

```
/画图 16:9 赛博朋克城市 --seed 42 --steps 30
/画图 一个白裙少女 --size 832x1216 --lora style/anime.safetensors:0.8
/画图 一只猫 --model juggernaut --hires 1.5
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
| `--hires` | `放大` | Hires Fix 放大倍数；`--hires 0` 表示本次关闭（可在配置页禁用单次覆盖） |
| `--hires-denoise` / `--hires-steps` | | 第二轮重绘幅度 / 步数（0 = 与首轮相同） |
| `--denoise` | `重绘` | 图生图的重绘幅度（0.05~1） |
| `--provider` | | 仅 `/反推` 用：本次用哪个提供商看图 |
| `--画` | `--draw` | 仅 `/反推` 用：反推后直接按结果出图 |

尺寸会自动对齐到 8 的倍数，并限制在所选架构的像素预算内。

---

## 配置

| 分组 | 说明 |
|------|------|
| `server` | ComfyUI 地址、出图等待上限、轮询间隔、排队补偿上限 |
| `llm_settings` | 是否启用提示词优化、指定 provider、或自定义 OpenAI 兼容接口 |
| `draw_settings` | 兜底出图参数、强制架构、质量词开关、负面词策略、强制 VAE、默认负面词 |
| `hires` | Hires Fix：文生图开关、图生图开关、是否允许单次 `--hires`、倍数、重绘幅度与步数 |
| `vision_settings` | 反推专用模型：指定一个支持看图的提供商，或填自定义 OpenAI 兼容接口 |
| `i2i` | 图生图：是否跟随 `/画图`、默认重绘幅度、输入图最长边、上传子目录 |
| `output` | 群聊 @ 触发人、附带参数信息、图片保留数量与天数 |
| `permission` | 白名单 / 黑名单 / 每日上限 / 冷却 / 管理员豁免 |
| `agent` | 无指令出图开关 |

> 管理员身份沿用 **AstrBot 自带的管理员设置**，插件不再单独维护一份名单。
> 打开「高级 → 启用无指令出图」后，对话模型会获得一个 `generate_image` 工具，
> 用户直接说「画一张雪夜里的白猫」即可触发（需要模型支持工具调用，默认关闭）。

**数据目录**：`data/plugin_data/astrbot_plugin_comfyui_smart/`
—— `catalog.json`（模型清单缓存）、`stats.json`（出图记录，供画廊回看与复现）、
`quota.json`（每日计数与冷却，**重启不清零**）、`workflows/`（你自己的模板）、`output/`（图片）。
所有写入都是「临时文件 + 原子替换」并带并发锁，多人同时出图不会互相覆盖。

---

## 工作流模板

模板是数据，不是代码。内置四个：

| 模板 | 适用 | 说明 |
|------|------|------|
| `sd_checkpoint` | SD1.5 / SDXL / Pony / Illustrious | 单文件底模通用图，参数随架构注入 |
| `img2img_checkpoint` | 同上 | 图生图：`LoadImage` + `ImageScale` + `VAEEncode` |
| `flux_checkpoint` | 一体化 Flux 单文件底模 | 模型/文本编码器/VAE 打包在一起的版本 |
| `flux_unet` | 分离权重的 Flux | `diffusion_models` 的 UNET + `text_encoders` 的 CLIP + 独立 VAE |

### 架构自适应

| 架构 | 默认分辨率 | 步数 | CFG | 采样器 / 调度器 | 负面词 |
|------|-----------|------|-----|----------------|--------|
| SD1.5 | 512x768 | 25 | 7.0 | dpmpp_2m / karras | 生效 |
| SDXL | 1024x1024 | 28 | 6.0 | dpmpp_2m / karras | 生效 |
| Pony / Illustrious | 1024x1024 | 28 | 7.0 | dpmpp_2m / karras | 生效（自动加 `score_*` 排除词） |
| Flux | 1024x1024 | 20 | 1.0 | euler / simple | **不生效**（改用 guidance 3.5） |
| 未识别 | 512x768 | 25 | 7.0 | dpmpp_2m / karras | 生效（按 SD1.5 处理） |

识别规则：名字含 `xl` / `juggernaut` / `realvis` → SDXL；含 `pony` / `illustrious` / `noobai` → Pony；
含 `flux` → Flux；含 `sd3` → SD3；**其余兜底 SD1.5**（社区惯例是 SDXL 系模型名里都带 `xl`）。
识别不准时用配置页的「强制指定架构」覆盖。

### 放自己的模板

把 **API 格式**的工作流 JSON 放进数据目录下的 `workflows/`（配置页「状态」页会显示完整路径），
刷新插件即可。两种写法都支持：

1. **裸工作流**（推荐）：直接是 ComfyUI 导出的 API 格式，顶层是 `{"节点id": {...}}`。
   注入点由**图结构**推导，不依赖节点 id 或标题 —— 见下一节。
2. **带元信息**：外面套一层 `{"name": ..., "arch": "flux", "loader": "unet", "graph": {...}}`。
   `arch` 可为 `sd15` / `sdxl` / `pony` / `flux` / `sd3` / `generic`，`loader` 为 `checkpoint` 或 `unet`。

---

## 注入点是怎么找到的

模板可以是任意来源的工作流，节点 id 千变万化（`3`、`KSampler_1`、`节点 27` 都常见），
所以插件**读图结构，不读节点编号**。三步依次尝试，前一步失败才走下一步：

**1）图语义推导（主路径）**
从采样器的 `positive` / `negative` 连线**向上游回溯**，找到第一个真正带文本输入的节点。
中间节点会被穿过（Flux 的 `FluxGuidance` 就夹在提示词与采样器之间）。
同一套语义推导还负责定位：尺寸节点（`EmptyLatentImage` / `EmptySD3LatentImage` / `ImageScale`）、
模型加载器（`CheckpointLoaderSimple` / `UNETLoader`）、`VAELoader`、`LoraLoader`、`SaveImage`。

**2）节点标题兜底**
用标题（ComfyUI 导出的 `_meta.title`，或顶层 `title`）匹配「正面 / 负面 / 尺寸 / 采样 / 保存」等关键词。
输入键不叫 `text` 的自定义提示词节点（如 `wildcard_text`）靠这一步定位。

**3）`bindings` 显式点名（最可靠）**
直接写死角色与位置，可写节点 id 或标题：

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

支持的绑定角色：`positive` `negative` `sampler` `latent` `model_loader`
`vae_loader` `lora_loader` `save` `guidance`。`input` 可省略，插件按节点结构推断。
**绑定写错在加载时就报错**（节点不存在、标题匹配不到、输入键不存在、角色名不认识），
不会拖到出图才失败 —— 错误会显示在配置页「状态」页与 `/模板列表`。

两条硬规则：

- 提示词与尺寸只会写进节点上**真实存在**的输入键，绝不凭空塞一个 `text` 或 `width` 字段
  —— 凭空塞会被 ComfyUI 以 `invalid_input_type` 拒绝。
- 尺寸写在**真正决定尺寸的那个节点**上：文生图写空潜空间节点，图生图则写在 `ImageScale` 上
  （图生图的潜空间来自 `VAEEncode`，它根本没有 `width` / `height`）。
  Hires Fix 的放大目标尺寸也按同一规则推导：读不到就改用按比例放大，
  **绝不瞎猜一个 512x512 把画面压变形**。

---

## 校验与能力探测

插件的原则是：**能在本地确定的，就不要丢给 ComfyUI 去拒绝**。分四层。

### 1）载入模板时：图结构校验

模板一载入就被严格校验 —— 每个节点的 `class_type` 与 `inputs` 必须存在，
每条连线（`[节点id, 槽位]`）的目标节点必须真的在图中，`bindings` 的每个角色都必须解析成功。
任何一条不过，该模板**不会被加载**，并在配置页「状态」页与 `/模板列表` 里列出原因。

同时会**剔除不可达节点**：从 `SaveImage` 反向做可达性分析，把游离在外的孤儿节点摘掉。
这一步不是洁癖 —— ComfyUI 会校验整张图里每个节点的 `class_type`，
一个残留的、缺自定义节点的孤儿节点会导致整个任务被拒绝。

### 2）成图前：节点能力探测

用 `GET /object_info` 拿到你 ComfyUI **已安装**的节点类名清单（缓存 5 分钟，不拖慢每次出图），
然后按顺序挑模板：按用途筛（文生图 / 图生图）→ 按加载方式筛（`checkpoint` / `unet`）→
排除缺节点的模板 → 按架构精确匹配。

若选中的模板仍缺节点，**根本不会提交**，直接告诉你缺哪个：

> 模板 sd_checkpoint 需要以下节点，但你的 ComfyUI 没有安装：SaveImage。

两项连带检查：

- **架构相容性**：Flux 模板不会被套到 SD 模型上，反之亦然。SD1.5 模型被误放进
  `diffusion_models` 目录时，会给出可操作的中文提示，而不是提交一张必然失败的坏图。
- **模型、VAE、LoRA 的取值**都对照真实清单校验（含子目录写法 `SDXL/xxx.safetensors`），
  报错时直接把**可用项**列出来。模型清单只统计真正的模型目录，
  `custom_nodes`、`configs`、`embeddings` 这类不算模型。

### 3）提交前：本地预检（只诊断，不拦截）

用你 ComfyUI 自己的 `/object_info` 输入约束，把要注入的参数先本地校验一遍
（下拉取值是否在可选项里、数值是否越界），把最常见的拒绝原因提前说清：

```
提交前的本地校验未通过（按你 ComfyUI 的输入约束检查）：
　· 节点 5（KSampler）的 sampler_name 取值 'dpmpp_2m' 不存在（可用项：euler、dpmpp_2m_sde）
　· 节点 5（KSampler）的 steps=0 小于最小值 1
```

**为什么只诊断、不拦截**：有些节点用 `VALIDATE_INPUTS` 自己校验输入
（典型是 `LoadImage`，它接受 `子目录/文件名` 这种不在下拉列表里的写法），
拿下拉清单硬卡会误杀完全合法的请求。所以**服务端始终是最终裁判**，
预检结论只在服务端拒绝时作为线索一并给出。

### 4）失败时：让下一次失败自证

少数 ComfyUI 构建（含各类整合包）拒绝出图时只回一句 `prompt_outputs_failed_validation`，
`details` 与 `node_errors` 都是空的，报错本身没有任何信息。遇到这种情况插件会：

- 把**实际提交的工作流**完整落盘到数据目录的 `last_failed_prompt.json`
  （含时间、插件版本、ComfyUI 地址、错误原文与 `graph`），可直接发出来定位；
- 记录正向提示词长度、所用模板、底模与 LoRA；
- 提示你去看 ComfyUI 控制台里 `Failed to validate prompt for output` 附近的日志。

正常失败则会把节点级原因翻译成中文，例如：

```
提交失败：Prompt outputs failed validation
　· Return type mismatch between linked nodes: model, MODEL != CLIP
　· 节点 8（LoraLoader）：lora_name 取值 'ghost.safetensors' 不存在（可用项：a.safetensors、b.safetensors）
```

---

## 排错

| 现象 | 处理 |
|------|------|
| 「没有发现任何模型」 | 确认配置页里的 ComfyUI 地址正确，且 `models/` 下有模型；点「刷新模型」 |
| 「提交失败：缺少自定义节点：XXX」 | ComfyUI 端确实缺该节点，或工作流里残留了孤儿节点（插件已会自动剔除可达性之外的节点） |
| 报错里 `node_errors` 与 `details` 都是空的 | 插件已把实际提交的工作流存成 `last_failed_prompt.json`，把这个文件与 `/状态` 的输出发出来即可定位 |
| 出图分辨率 / CFG 明显不合适 | 架构识别不准。在配置页填「强制指定架构」`sd15` / `sdxl` / `pony` / `flux` / `sd3`；出图消息里会显示识别结果 |
| 手部崩坏、多手指 | 先确认架构正确（**SD1.5 被当成 SDXL 跑 1024 档是最大成因**），再开 Hires Fix（`--hires 1.5`），并在描述里写清手部动作（「双手抱胸」比事后加负面词有效） |
| 出图偏色、发灰、发绿 | 多为底模自带 VAE 有问题。把 `vae-ft-mse-840000-ema-pruned.safetensors` 放进 `models/vae`，再在配置页「强制使用指定 VAE」填该文件名 |
| 颜色/构图每次都不一样 | 正常现象。`--seed 12345` 即可复现；出图消息与画廊里都会显示本次种子 |
| 提示词太长没效果 | CLIP 每段只编码 77 token。ComfyUI 对超长提示词**不是截断**而是分段编码后拼接，所以长负面词依然生效、只是每个词的影响力被稀释；把最关键的词放前面 |
| `/反推` 的结果和图对不上 | 几乎都是**所用模型不支持看图**：AstrBot 会把图片换成字面量 `[Image]`，模型就开始编。发 `/状态` 看「看图」那一行；不支持时在配置页「反推专用模型」里选一个支持看图的提供商或填自定义接口，也可临时用 `/反推 --provider <id>` |
| 想看某张图的提示词和参数 | 配置页「统计」页的画廊里**点击图片**，弹窗可看可复制 |
| 「出图超时」 | 提高「出图等待上限」；前方排队任务多时，插件已按队列长度自动补偿等待时间 |
| Flux 出图忽略了负面提示词 | 这是 Flux 的正常行为（CFG=1.0），请改用正向提示词或 `guidance` |

启动时插件会打印一条横幅，用来确认「跑的到底是哪一版」：

```
ComfyUI 智能绘图 v0.6.3 已激活｜模板 4 个｜数据目录 …｜日志走 astrbot.api.logger｜Pages 已注册｜排队补偿上限 10 个任务
```

---

## 更新日志

**v0.6.3** — 上架规范整改：日志统一走官方 logger

- **背景**：上架审查退回。`main.py` 里对 `self.logger` 做了回退 —— 当 AstrBot 基类没有该属性时
  执行 `import logging` + `logging.getLogger("astrbot")`。规范要求 **logger 必须且只能来自
  `from astrbot.api import logger`，严禁使用 Python 内置 logging**。
- **整改**：模块顶层统一改为 `from astrbot.api import AstrBotConfig, logger`，删除整个回退分支与
  `_has_plugin_logger` 标记，插件内 28 处日志调用改用该 logger。已核对 `astrbot.api.logger`
  在 **4.26.0**（由 `from astrbot import logger` 转发）与 4.27.3、4.28.1 上均存在，回退本就多余。
- 启动横幅里的「插件专属日志 可用」改为「日志走 astrbot.api.logger」，不再声称一个 4.26 上
  并不存在的特性。
- **新增守卫**：测试会扫描插件源码，一旦再次出现 `import logging` / `logging.xxx` 立即失败；
  API 面核验把 `astrbot.api.logger` 列为必须导出项。

**v0.6.2** — Hires Fix 细分开关 + 反推可指定专用模型

- **Hires 拆成两个开关**：`hires.enable`（文生图自动走 Hires）与
  `hires.enable_for_i2i`（图生图是否也走，默认开）。此前只有一个开关，
  开了以后图生图也会被强行放大，而图生图放大往往更糊、更慢。
- **`hires.allow_inline`**：是否允许单次 `--hires` 覆盖配置。关掉后 `--hires` 会被忽略并按配置执行，
  日志写明 `单次 --hires 已被配置禁用，本次按配置设置处理`；`--hires 0` 单次关闭始终可用。
- **新增「反推专用模型」（`vision_settings`）**：可单独指定一个支持看图的 AstrBot 提供商，
  或填自定义 OpenAI 兼容接口（地址 / Key / 模型名），完全不经过 AstrBot 的提供商体系
  （适合本地 Ollama、vLLM、第三方 VL 接口）。优先级：`/反推 --provider` > 自定义接口 >
  反推专用提供商 > 对话默认提供商（旧配置 `llm_settings.vision_provider` 仍兼容）。
- **修复：图生图的 Hires 曾把画面压变形**。图生图的潜空间来自 `VAEEncode`（没有宽高输入），
  旧代码取不到尺寸就按 512x512 兜底，于是 512x768 的图被 `LatentUpscale` 硬拉成 768x768。
  现在沿连线上溯读取缩放节点的目标宽高；连尺寸都拿不到时改用 `LatentUpscaleBy` 按比例放大。
  真机实测：修复前产出 768x768（宽高比被改），修复后 768x1152，`status_str = success`。

**v0.6.1** — 反推：模型看不见图时明确报错，而不是编内容

- **根因**：AstrBot 依据 `provider.provider_config["modalities"]` 判断提供商是否支持图片，
  **若该列表不含 `image`，图片会被替换成字面量 `[Image]` 再发给模型**，
  模型只看到「请看这张图…[Image]」，于是照常编出一段描述，而插件把它当成反推结果返回。
- 现在会先检查看图能力：不支持就直接报错并告诉你去哪里配，不再返回编造的内容。
- 新增 `/反推 --provider <id>` 临时指定看图模型；`/状态` 显示当前对话模型能否看图
  （`✅ 支持` / `❌ 不支持` / `❔ 未知`）；反推结果里标注**实际使用的看图模型**。

**v0.6.0** — 新增「图生图」

- **`/图生图 <描述>`**（别名 `/改图` `/i2i`）：以你发的图为底按描述重绘，`--denoise` 控制重绘幅度。
- 带图发 `/画图` 会自动走图生图；`i2i.enable` 可关闭这一行为。
- 新增 `img2img_checkpoint` 模板与 `POST /upload/image` 上传链路；输入图按最长边自动缩放。
- `denoise` 语义经真机实测标注：0.3 → 约 26% 像素改变，0.55 → 约 76%，0.9 → 约 95%。

**v0.5.0** — 新增「看图反推提示词」

- **`/反推`**：把图片和指令一起发、或回复一张图再发 `/反推`，插件把图片交给支持看图的对话模型，
  反推出可直接使用的英文 tag（含人物数量、外貌、服装、动作与**手部状态**、镜头与画风）。
- **`/反推 --画`**：反推后直接按结果出图；提示词不再经 LLM 二次改写（避免英文 tag 被改坏）。
- **Hires Fix**（同时发布）：首轮出小图 → 潜空间放大（`LatentUpscale`）→ 二次采样重绘，
  只用核心节点，**不需要额外的放大模型**（ESRGAN 之类）。

**v0.4.0** — 新增 Hires Fix（放大重绘）

- 配置页新增「Hires Fix」卡片，也可对单次生效：`--hires 1.5`、`--hires-denoise 0.5`、
  `--hires-steps 12`；`--hires 0` 单次关闭。默认**关闭**（出图时间约翻倍）。
- 第二轮继承首轮的模型、正负条件、CFG、采样器与调度器，只改潜空间来源、`denoise` 与种子；
  放大后尺寸自动对齐 8 的倍数。目标像素超过 4.2 MP 时会在日志与出图消息里提示显存风险。
- 真机实测：512x768 → 放大 1.5 倍 → 产出 768x1152，`status_str = success`。

**v0.3.7** — 负面词归一化去重 + 长度诊断 + `raw` 档

- **归一化去重**：`((extra limbs))`、`(extra limbs)`、`extra limbs`、`[[extra limbs]]` 在 ComfyUI 里
  是同一个词，此前被当成四个标签全部保留。现在按「剥掉括号与显式权重」后的键去重，保留权重最高的写法。
  实测某份流行长负面词（Deep Negative 系列）从 **99 个标签 / 约 435 token / 约 6 段编码**
  降到 **46 个 / 约 230 token / 约 3 段**，且不丢任何**不同的**词。
- **纠正一个常见误解**：ComfyUI 对超长提示词**不是截断**，而是切成多段分别编码后拼接
  （对照 `comfy/sd1_clip.py` 的 `torch.cat(embeds_out)` 确认），所以长负面词依然生效，
  只是每个词的影响力被稀释。日志记录每次的正负提示词长度，负面词超过约 3 段编码时在出图消息里提示精简。
- 新增 `raw` 档：严格原样使用你的负面词，连去重都不做。

**v0.3.6** — 负面提示词策略可完全自定义

`draw_settings.negative_mode` 四个档位：

| 档位 | 效果 |
|---|---|
| `merge`（默认） | 默认词 + 手部/肢体规避词 + 架构附加词 + LLM 负面词，合并去重 |
| `guard_only` | 你的默认词 + 手部/肢体规避词；不采纳 LLM 的负面词 |
| `custom_only` | 只用你自己的词，不追加任何内容，只做重复写法合并 |
| `raw` | 严格原样，一个字符都不改 |

行内 `--negative "..."` 在前三档下与其它词合并，在 `raw` 下直接覆盖。

> 取舍提醒：`bad hands`、`extra fingers` 这些词正是改善手部崩坏的机制之一，
> 完全去掉（`custom_only`）手可能更差；想两者兼顾就用 `guard_only`。

**v0.3.5** — 修复画廊页报错，并给前端补上真正的执行测试

- **修复「统计读取失败：gallery is not defined」**：v0.3.4 把变量 `gallery` 改名为 `galleryItems`
  时漏改了判断条件里的一处引用，导致统计页整页报错。
- **补上前端执行测试** `tests/test_pages_js.js`。此前 `app.js` 只做过 `node --check` 语法检查，
  而语法检查**查不出「引用了已改名或不存在的变量」**这类错误 —— 这正是本次事故溜过去的原因。
  现在用最小 DOM 桩把 `app.js` 真正跑起来，断言画廊条目数、最新一张排在最前、
  弹窗字段、老记录（无 `params`）仍能打开、剪贴板不可用时退化为选中文本等。
  已用「把修复回退」反向验证：它会精确复现原报错。

**v0.3.4** — 画质优化 + 画廊详情

针对「手穿模、多手指、颜色变化」找到并修掉三个具体成因：

- **负面提示词曾被整体替换**：旧逻辑是「LLM 返回了负面词就整体覆盖默认词」，
  于是 `bad hands` / `extra fingers` 被悄悄丢掉 —— 这正是多手指的直接来源。
  现在改为**合并**（默认词 + 手部肢体规避词 + 架构附加词 + LLM 词 + 行内参数，去重保序）。
- **LLM 给的尺寸会绕过架构适配**：LLM 返回的 width/height 优先级高于架构档案，
  于是 SD1.5 模型又被拉回 1024 档。现在 LLM 的尺寸**只当比例意图**，
  总像素按所选架构的预算归一（如横构图 1344x768 → 824x472）；行内 `--size/--ratio` 仍原样生效。
- **指定的 VAE 曾被静默忽略**：`sd_checkpoint` 用底模自带 VAE、没有 `VAELoader` 节点，
  而注入逻辑写的是「模板有 `VAELoader` 才写入」。现在会自动插入 `VAELoader` 并改接 `VAEDecode`。
- 另按架构自动补质量词（SD1.5 / Pony 加，SDXL / Flux 不加），提示词里要求 LLM 明确描述手部动作。
- **画廊详情**：点击作品可看大图、完整正负面提示词与全部参数，并可一键复制。

**v0.3.3** — 修复「选到 LoRA 就出图失败」的根因（真机复现并验证）

- **选中 LoRA 时提交的工作流会形成依赖环**，被 ComfyUI 拒绝且只回一句
  `prompt_outputs_failed_validation`（`details` 与 `node_errors` 都是空的）。根因是 `_apply_lora`
  在插入 `LoraLoader` 后重连下游时**把刚插入的节点自己也算了进去**，把它的 `clip` 改写成了指向自身：

  ```
  修复前: "8": {"class_type": "LoraLoader", "inputs": {"clip": ["8", 1], ...}}   # 指向自己
  修复后: "8": {"class_type": "LoraLoader", "inputs": {"clip": ["1", 1], ...}}   # 指向底模
  ```

  这也解释了「短提示词正常、长提示词报错」：提示词越长，LLM 越容易挑一个 LoRA。
  真机验证：修复前 HTTP 400，修复后 HTTP 200 并成功出图。同时给 `validate_graph` 加了自环检查。
- **架构识别大幅改进**：原来未识别时兜底 `sdxl`，实测在某用户 121 个底模上把 115 个判成 SDXL
  （其中绝大多数其实是 SD1.5 时代模型）。现在兜底 SD1.5，同一批模型识别结果变为
  **101 sd15 / 18 sdxl / 2 pony**，且判为 SDXL 的名字里确实都带 `xl`。

**v0.3.2** — 让「服务端不说原因」的失败变得可排查

背景：拿到 `{"error": {"type": "prompt_outputs_failed_validation", "details": ""}, "node_errors": {}}`
—— 两个字段都是空的。对照 ComfyUI 上游 `execution.py` 逐行追过 `validate_prompt` / `validate_inputs`，
确认按上游代码这个组合在逻辑上不可达，因此判断是对方的构建存在差异。与其继续猜，改为让它自证：

- **提交前本地预检**（用服务端自己的 `/object_info` 约束校验注入的参数）
- **失败工作流落盘** `last_failed_prompt.json`、失败日志补全提示词长度与模型信息
- **`/状态` 显示 ComfyUI 版本**，便于识别整合包 / 分支构建

**v0.3.1** — 修复三个实际使用中暴露的问题

- **Pages 全部接口返回「未找到该路由」**：重写时漏了在主模块里调用 `register_pages_routes`，
  而测试里手动调用了一次，正好把漏接遮住了。现在改为**构造插件时自动注册**，
  并加了回归守卫（不再手动调用，直接断言构造即注册）。
- **模型数量虚高（例如 2935 个）**：ComfyUI 的 `GET /models` 返回的是 `folder_names_and_paths`
  的**全部**键，其中 `custom_nodes` 不是模型目录，且它注册的扩展名白名单是**空列表**，
  而 `filter_files_extensions` 对空列表放行所有文件，于是递归 `custom_nodes` 会把每个节点包里的
  `.py/.js/node_modules` 全列出来。现在改为**模型目录白名单**，并排除 `custom_nodes`、`configs`、
  `datasets`、`embeddings`、`vae_approx`。
- 清掉两处「写了却从没接上」的死代码，并新增**死代码守卫测试**（静态检查所有函数是否真被调用，
  框架回调与 `@filter.*` 处理器除外）；模板改为在 `__init__` 中加载，不再依赖 `initialize()` 的时序。

**v0.3.0** — 架构级重写

修复（旧版这些缺陷导致插件实际上不可用）：

- **配置无法保存**：旧版在 Pages 保存时只替换内存 dict，还把官方配置对象换成了普通 dict，重启即回滚。
  现在保存走 `save_config_async()` 真正落盘。
- **保存会清空未提交的配置项**：改为**深度合并**，前端只提交自己管理的字段。
- **管理员死锁**：旧版 `admin_ids` 默认为空，而唯一的授权入口又要求「已是管理员」，
  新用户装完无法自举，`/刷新模型` 永远不可用。现在改用 AstrBot 自带的 `event.is_admin()`。
- **默认 LLM 回退必然抛异常**：`llm_generate` 的 `chat_provider_id` 是必填参数，旧版写了个不传它的
  回退分支。现在显式解析 provider，并在没有 LLM 时**退化为原描述出图**。
- **子目录模型被误杀**：旧版把含 `/` `\` 的模型名判为「无效」，而 ComfyUI 对子目录模型返回的正是
  `SDXL/xxx.safetensors`。现在按真实清单做白名单校验。
- **出图等待是无上限死循环**：旧版 `while True` 没有总超时。现在有硬超时、按队列长度补偿，
  并正确识别中断与执行失败。

新增：

- **工作流模板引擎**：模板成为可替换的数据文件，支持用户自带 API 工作流（图结构自动推导注入点）
- **架构自适应**：SD1.5 / SDXL / Pony / Flux 自动匹配分辨率、CFG、采样器、guidance
- **Flux 支持**：分离权重（`flux_unet`）与一体化底模（`flux_checkpoint`）两种模板
- **模型发现重写**：改用 `GET /models` + `GET /models/{folder}`，老版本回退到 `/object_info`
- **结构化错误**：解析 `node_errors` 翻译成中文，并给出「可用项」
- **按节点标题注入 + `bindings` 显式覆盖**、**节点能力探测**（缺节点直接报出缺哪个）
- **无指令出图**（可开关，默认关闭）、**配额落盘**、**图片保留策略**
- **修正版本声明**：原来写的 `>=4.9.2` 是错的（`astrbot.api.web` 依赖 FastAPI，自 v4.26.0 才有）
- **自带测试与 API 面核验器** `tests/check_api_surface.py`

移除：空转的「把几百个模型名分批喂给 LLM 生成风格标签」链路、从未被读取的死模板文件、
Pages 后端那套「猜 `file_response` 不存在」的三层降级、`/管理员` `/白名单` `/黑名单` 指令
（改由配置项承载且真正持久化）。

**v0.2.0** — 修复新版 ComfyUI 模型列表解析；新增模型池校验
**v0.1.0** — 第一版

---

## 开发与测试

```bash
# 逻辑测试：自带 AstrBot / aiohttp 最小桩，不依赖第三方库
python tests/test_logic.py

# 插件页前端测试：用最小 DOM 桩真正执行 app.js（需要 Node）
node tests/test_pages_js.js

# API 面核验：对照真实 AstrBot 源码，确认插件用到的每个符号与方法都存在
python tests/check_api_surface.py /path/to/AstrBot
```

测试覆盖参数解析、尺寸对齐、模型名校验、错误解码、模型发现的两条路径、模板推导与注入、
按标题注入与 `bindings` 覆盖、非法绑定拒绝、不可达节点剔除、节点能力探测与缺节点报错、
存储与配额、Pages 的合并保存语义、以及 mock ComfyUI 下的完整出图流程。

`test_pages_js.js` 是前端唯一能抓到「变量改名漏改」「渲染时抛错」的测试 ——
`node --check` 只验证语法，不检查标识符引用是否有效。

`check_api_surface.py` 不启动 AstrBot，而是直接读源码 AST，核验 import 的符号与调用的成员
（`Context` / `StarTools` / `AstrBotConfig` / `AstrMessageEvent` / `Image` / `astrbot.api.logger` /
Pages 请求代理与响应 helper）是否仍然存在，用于 AstrBot 升级后复检，
避免「某个方法改名后插件静默失效」。已在 **v4.26.0**、**v4.27.3**、**v4.28.1** 上跑通。

## 👥 贡献指南

- 🌟 Star 这个项目！
- 🐛 提交 Issue 报告问题
- 🔧 提交 Pull Request 改进代码
