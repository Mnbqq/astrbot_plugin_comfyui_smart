![:name](https://count.getloli.com/@astrbot_plugin_comfyui_smart?name=astrbot_plugin_comfyui_smart&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# AstrBot ComfyUI 智能绘图插件

让 AstrBot 连接 ComfyUI，用一句自然语言就能出图：**「/画图 一个白裙少女站在樱花树下」** → LLM 自动优化提示词、挑选模型、构建工作流并返回图片。

---

## 功能

- 🎨 **自然语言出图**：中文描述 → 英文提示词 → 自动出图
- 🧠 **LLM 智能选型**：从你 ComfyUI 里的真实模型中推荐 Checkpoint / LoRA
- 🛡 **模型池校验**：只使用 ComfyUI 真实存在的模型，杜绝 LLM 编造导致提交失败
- 🔌 **兼容新旧版 ComfyUI**：`/object_info` 的两种返回格式都能正确解析
- 🔐 **权限管理**：管理员 / 白名单 / 黑名单 / 每日限额 / 冷却
- 📊 **统计与画廊**：记录出图历史，配置页可回看作品
- 💬 **群聊 @ 触发人**，私聊直接出图
- 🖥 **可视化配置页**：浏览器中配置，无需改 JSON

---

## ⚠️已发现，待修复内容

1. 从可视化配置页内，配置无效

---

## 快速开始

1. 安装插件并重启 AstrBot
2. 配置页填写 ComfyUI 地址（如 `http://127.0.0.1:8188`）
3. 发 `/刷新模型`（管理员）拉取并分析模型
4. 发 `/画图 你的画面描述` 出图

> LLM 未单独配置时，默认调用 AstrBot 全局 LLM。

---

## 指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/画图 <描述>` | 生成图片 | 默认开放 |
| `/刷新模型` | 从 ComfyUI 拉取并分析模型 | 管理员 |
| `/模型列表` | 查看已保存模型 | 所有人 |
| `/统计` | 查看使用统计 | 所有人 |
| `/管理员 添加\|移除 <QQ号>` | 维护管理员 | 管理员 |
| `/白名单 /黑名单 添加\|移除 <QQ号>` | 维护名单 | 管理员 |
| `/帮助` | 帮助 | 所有人 |

---

## 配置

| 分组 | 说明 |
|------|------|
| `server` | ComfyUI 地址 `base_url`、超时 `timeout` |
| `model_switch` | checkpoint / controlnet / vae / lora 是否参与调用 |
| `llm_settings` | 自定义 LLM（provider / api_key / base_url / model），留空用默认 |
| `draw_settings` | 默认宽高、步数、采样器、CFG、负面词 |
| `output` | 群聊是否 @ 触发人、是否保存图片 |
| `permission` | 管理员 / 白名单 / 黑名单 / 每日上限 / 冷却 |

---

## 数据存储

目录：`data/plugin_data/astrbot_plugin_comfyui_smart/`

| 文件 | 说明 |
|------|------|
| `models_analyzed.json` | 模型列表（由 `/刷新模型` 生成） |
| `stats.json` | 统计与出图记录 |
| `output/` | 生成的图片 |

> ⚠️ 不要手动编辑或使用第三方预设模型缓存，模型名必须以 `/刷新模型` 从你 ComfyUI 拉取到的为准。

---

## 常见问题

| 现象 | 处理 |
|------|------|
| 提示「未拉取到任何模型」 | 确认 ComfyUI 地址可访问；升级到 v0.2.0（修复了新版 ComfyUI 解析） |
| 报错 `xxx not in (list...)` | 模型不存在。删掉缓存后重新 `/刷新模型` |
| 报错 `not in []`（VAE） | ComfyUI 没有独立 VAE，v0.2.0 会自动改用 Checkpoint 内置 VAE |
| `/刷新模型` 后列表没变 | 先写缓存再分析，多个模型分析需几分钟，稍候即可 |

---

## 更新日志

**v0.2.0**
- 修复新版 ComfyUI 模型列表解析（兼容 `ckpt_name[0]` 格式）
- 新增模型池校验，出图只使用 ComfyUI 真实存在的模型
- `/刷新模型` 先保存真实列表再分析，失败不丢数据

**v0.1.0**
- 第一版测试


## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码
