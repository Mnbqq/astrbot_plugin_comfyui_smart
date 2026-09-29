![:name](https://count.getloli.com/@astrbot_plugin_comfyui_smart?name=astrbot_plugin_comfyui_smart&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

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

## 命令

| 命令 | 说明 |
|---|---|
| `/画图 <描述>` | 文生图 |
| `/图生图 <描述>` + 图片 | 图生图（`--denoise` 重绘幅度） |
| `/扩图 <描述>` + 图片 | 扩图（`--left/--right/--top/--bottom/--feather`） |
| `/视频 <描述>` | 文生视频（`--seconds/--fps/--length/--steps/--cfg`） |
| `/图生视频 <描述>` + 图片 | 图生视频（发两张图 = 首尾帧） |
| `/反推` + 图片 | 看图反推提示词 |
| `/取消`（`/取消 全部`） | 取消自己的任务（管理员可取消全部） |
| `/状态` | ComfyUI 是否在线、队列、显存 |
| `/模型列表` `/刷新模型` `/模板列表` | 模型与模板 |
| `/统计` | 出图次数、模型使用、最近记录 |
| `/帮助` | 用法速查 |

行内参数（可组合）：`--model 底模 --lora 名:0.8 --vae 名 --size 1024x1024 --steps 28 --cfg 7 --seed 123 --ratio 16:9`
提示词开关（本次生效）：**`--llm` 强制让 AI 改写 / `--no-llm` 本次不改写**

## 配置要点

| 分组 | 关键项 |
|---|---|
| `general` | 语言（auto / zh-CN / en-US） |
| `server` | ComfyUI 地址、等待上限、**换大模型前自动卸载**（默认开） |
| `queue` | 同时出图上限、单人上限、排队超时 |
| `llm_settings` | 图片提示词优化（中文→英文 tag）、**视频提示词也交给 AI 改写**（`optimize_for_video`）、看图模型 |
| `draw_settings` | 默认底模/负面/尺寸/步数、强制 LoRA/VAE、架构覆盖 |
| `hires` | 高清修复 |
| `i2i` | 图生图的 `--denoise` 默认值、输入图上限 |
| `video` | **机器档位**、**文生视频模型**、**图生视频模型**、时长/帧率/上限、是否直接发视频 |
| `output` | 图片格式、保留数量、压缩 |
| `permission` | 黑白名单、冷却、每日次数 |
| `backends` / `agent` | 多台 ComfyUI 分流；LLM 工具开关 |

### 视频模型按功能分开配

- `video.t2v_model` → **文生视频**默认模型（留空 = 自动挑第一个视频权重）
- `video.i2v_model` → **图生视频**默认模型（Wan 2.2 TI2V-5B 两者共用，可填同一个）
- 聊天里 `--model xxx` 优先级最高。

## 机器档位与推荐模型

`video.machine` 决定**分辨率 / 帧数 / 步数上限**，防止低配机器被打爆。填 `auto`（默认）按显存自动判定：≤10G → low，12~20G → mid，≥24G → high；**内存不足 20G 再降一档**；探测不到就按最低档保守处理。

| 档位 | 硬件标准 | 视频上限 | 推荐视频模型 | 推荐图片模型 |
|---|---|---|---|---|
| **低配** | 显存 ≤10G **或** 内存 ≤16G | 480p / 97 帧 / 12 步 | Wan 2.2 TI2V-5B **Q4_K_M GGUF**（质量）· **Turbo Q4 GGUF**（快 3.8 倍）· LTXV 2B GGUF（最省）· Wan 2.1 T2V 1.3B | SD1.5 系（最快）· Animagine XL 4.0 / RealVisXL V5（SDXL，1024 只要 31 秒） |
| **标配** | 显存 12~20G **且** 内存 32G | 720p / 121 帧 / 30 步 | Wan 2.2 TI2V-5B **fp16**（单模型 9.3GB）· 同款 Q8 GGUF | 上面全部 · Flux.1 schnell GGUF（4 步） |
| **高配** | 显存 ≥24G **且** 内存 ≥64G | 720p / 161 帧 / 50 步 | 同上 fp16 + 更长片段（14B 是双模型两段式，需另加模板） | 上面全部 · Flux dev fp16 · Qwen-Image（建议 ≥32G 内存） |

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

## AI 改写提示词（图片 / 视频两套）

| 用途 | 改写目标 | 开关 | 说明 |
|---|---|---|---|
| 图片 | 英文 tag 堆叠 + 选底模/LoRA/VAE | `llm_settings.enable_prompt_optimize` | 中文提示词在 SD1.5/SDXL 上基本无效，靠它翻译 |
| **视频** | **动作 + 镜头 + 光影**（保持你的语言） | `llm_settings.optimize_for_video` | 视频要的是「她缓缓回头、镜头推近」，不是 tag；视频模型由 `video.t2v_model` / `i2v_model` 决定，**LLM 只改提示词、不选模型** |

聊天里可以临时覆盖：`--llm` 强制改写、`--no-llm` 本次不改写（对图片和视频都生效）。

## 中文提示词

| 用途 | 中文 | 说明 |
|---|---|---|
| 视频（Wan / LTXV） | ✅ 直接写 | 一定要写**动作**：「她缓缓回头、长发飘动、镜头缓推」 |
| 图片（SD1.5 / SDXL） | ❌ 基本无效 | 打开 **LLM 提示词优化**（自动转英文 tag），或直接写英文 |
| 图片（Flux） | ⚠️ 能出图但丢细节 | 建议英文 |

## 模板（13 个，放 `workflows/` 即生效）

| 文件 | 用途 |
|---|---|
| `sd_checkpoint` · `img2img_checkpoint` · `outpaint_checkpoint` · `inpaint_checkpoint` | 文生图 / 图生图 / 扩图 / 局部重绘 |
| `flux_checkpoint` · `flux_unet` · `flux_schnell_gguf` | Flux：一体化 / 分离权重 / GGUF 4 步 |
| `lumina_checkpoint` | Lumina-Image-2.0（1024 / 30 步） |
| `wan_t2v` · `wan_i2v` | Wan 2.x（safetensors）文生 / 图生视频 |
| `wan22_t2v_gguf` · `wan22_i2v_gguf` | Wan 2.2 TI2V-5B（GGUF，低配首选） |
| `ltxv_t2v` | LTX-Video 2B（GGUF，最省显存） |

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
| 多人同时用越来越慢 | 调 `queue.max_concurrent` / `per_user_limit`；ComfyUI 本身是串行执行，并发主要买的是公平与队列秩序 |

## 更新日志（最近）

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

## 许可

MIT
