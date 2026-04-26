# 烟雨江湖助手

一个基于 PySide6 + MuMu 模拟器的《烟雨江湖》自动化工具。通过 ADB 注入点击、归一化坐标抽象、OCR 闭环校验,把日常重复操作(刷副本、跑商、买药、采集)封装成可视化编辑、可中断的"流程"(routine)。

不依赖游戏内存、不修改客户端,纯外挂方式(屏幕截图 + ADB tap)与游戏交互。

---

## 目录

- [适用场景](#适用场景)
- [快速开始](#快速开始)
- [核心概念(5 分钟入门)](#核心概念5-分钟入门)
- [配置体系](#配置体系)
- [步骤类型清单](#步骤类型清单)
- [模板与预设系统](#模板与预设系统)
- [项目结构](#项目结构)
- [架构与运行时](#架构与运行时)
- [跨分辨率支持](#跨分辨率支持)
- [OCR 闭环](#ocr-闭环)
- [开发指南](#开发指南)
- [已知限制](#已知限制)

---

## 适用场景

适用:

- MuMu 模拟器(Windows 上)运行的《烟雨江湖》PC 版
- 想自动化「跑路 → 找 NPC → 进副本 / 买东西 / 收集物品 → 切图 → 重复」这类有明确路线的循环任务
- 需要可视化编辑流程,而不是写脚本

不适用:

- 真机 / 其他模拟器(没测过,Mumu 探测逻辑写死了)
- 战斗自动化、PVP、需要精细时序的微操(本项目走的是粗粒度宏,带 OCR 兜底)
- 检测对抗(本项目不做反检测,如果游戏检测了会触发风控)

---

## 快速开始

### 0. 准备

- Windows 10/11
- 安装 [MuMu 模拟器 12](https://www.mumuplayer.com/),启动并登录烟雨江湖,进入游戏世界(任意地图)
- Python 3.10+

### 1. 安装

```bash
git clone <repo>
cd python  # 项目根
pip install -r requirements.txt
```

`requirements.txt` 里的关键依赖:`PySide6`、`opencv-python`、`scikit-image`、`pywin32`、`PyYAML`、`Pillow`、`pynput`。

### 2. 启动

```bash
python main.py
```

主窗口会自动探测 MuMu 安装路径(从注册表)+ 实例 0 的 adb 端口,初始化 `Mumu` 对象。

### 3. 第一次使用的标准流程

按顺序点击主菜单的几个按钮:

1. **运动配置** — 录入 UI 元素的归一化坐标(背包按钮、空白处、商店项栅格等),以及视野档位的格子大小。这是所有 routine 的基础。
2. **添加地图信息** — 录入大地图上各地点图标的像素坐标 + 跳转按钮相对偏移。每个地点录一次即可,跨 routine 共享。
3. **编辑 Routine** — 用可视化编辑器组装步骤,保存为 yaml。
4. **执行 Routine** — 选择 yaml,启动。支持暂停 / 单步 / 中断。

每一步都有专门的对话框,UI 里有提示,不会写代码也能完整使用。

---

## 核心概念(5 分钟入门)

### 归一化坐标

整个项目的对外接口**只用归一化坐标** `(x, y) ∈ [0, 1]²`,参考系是 MuMu 渲染窗口的客户区(纯游戏画面,不含模拟器装饰条、不含全屏模式的左右壁纸填充)。

- `Mumu.click((0.5, 0.5))` 表示点击渲染区正中心
- 内部自动 `× device_w × device_h` 转成 ADB tap 的像素坐标
- 同一份 yaml 在 1080p / 2K / 全屏 / 窗口模式下都能直接运行,无需迁移

### Routine

一个 yaml 文件,描述一组顺序执行的步骤。例:

```yaml
name: 刷蛇
description: 洛阳 → 黑水沟 → 刷怪 → 回城卖装备
loop_count: 0           # 0 = 无限循环
loop_interval: 5.0      # 每轮间隔 5 秒
starting_map: 洛阳      # 第一步若是 travel, 必须指定起始地图
steps:
  - {type: travel, to: 黑水沟}
  - type: move
    at_map: 黑水沟
    path:
      - [10, 18]
      - [14, 22]
  - {type: button, name: chat_1, skip: 3}
  - {type: sleep, preset: travel_transition}
  - ...
```

### 步骤类型

主要有 11 种(见 [步骤类型清单](#步骤类型清单))。每种 step 有自己的字段:

- `move` 走一段路径
- `travel` 大地图传送到某地点
- `click` 点一个归一化坐标
- `button` 点 chat_N / table_N 这类菜单按钮
- `buy` 在购买界面循环购买
- `sleep` 等待秒数
- `wait_pos_stable` 等待小地图坐标稳定
- `include` 串联另一个 routine
- ...

---

## 配置体系

所有配置都在 `config/` 下,分为「公共配置」和「routine」两类。

```
config/
├── common/
│   ├── movement_profile.yaml      # 运动配置: UI 坐标、视野档位、ClickDelays、模板
│   ├── map_switch_btn_position.yaml  # 大地图地点信息: icon 像素位置 + 跳转 btn 偏移
│   └── map_registry.py            # 地图 schema 定义 + 默认地名
├── routines/
│   ├── 刷蛇.yaml                   # 用户 routine
│   ├── 黑水沟去帝陵.yaml
│   └── ...
└── templates/
    └── minimap_coord/             # OCR 字符模板 (0~9, 括号, 逗号)
```

### `movement_profile.yaml` 是什么?

一个**全局**配置文件,记录所有 routine 共享的"游戏世界参数":

| 字段                | 含义                                                                        |
| ------------------- | --------------------------------------------------------------------------- |
| `character_pos`     | 角色 sprite 在屏幕的归一化中心位置(默认 ~0.43, 0.49)                        |
| `vision_sizes`      | 视野档位(小/中/大),每档的格子归一化大小 + 单段最大走多少格                  |
| `ui_positions`      | 各 UI 元素的归一化坐标:背包按钮、空白处、chat_N 等距按钮组、商品栅格等      |
| `click_delays`      | 各类点击之后的等待秒数(按场景分类:button、blank_skip、travel_transition...) |
| `click_templates`   | 用户自建的 click 行为模板(整体打包 位置+skip+delay)                         |
| `button_templates`  | 同上,但是 button 步骤                                                       |
| `minimap_coord_roi` | 小地图坐标数字的 OCR 识别区域(归一化 ROI)                                   |
| `map_view_area`     | 屏幕上"地图能完整显示的矩形",避开周围 UI 遮挡                               |

注意:`movement_profile.yaml` **不再按分辨率分桶**(这是项目早期的设计)。所有字段都是归一化的,跨分辨率通用。

### 设计哲学:为什么这样分?

- **`movement_profile`**:游戏世界参数,改一次,所有 routine 受益
- **每个 routine yaml**:具体业务流程,只引用 movement_profile 里的预设和模板,不复制坐标

例如:你在 movement_profile 改了「跳转过场延时」(`travel_transition: 2.5 → 3.0`),所有引用这个预设的 routine 立即生效,不用挨个改 yaml。

---

## 步骤类型清单

所有步骤继承自 `Step`,定义在 `app/core/routine.py`。每个步骤都可选 `at_map` 字段(运行时校验当前地图,防止串图)。

| `type`               | 字段                                                    | 说明                                                                                                |
| -------------------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `move`               | `path: [(x,y), ...]`                                    | 在当前地图内走路径。第一个点是起点,后续逐段走;`[-1, -1]` 是飞行段(轻功)                             |
| `travel`             | `to: str`                                               | 大地图传送。打开背包 → 点票券 → 在大地图上点目标 icon → 点确认                                      |
| `enter_map`          | `map: str`                                              | 宣告"已切到某地图",只更新 runner 的 `_current_map`,不发指令(用于过地图边界后告诉后续 move 新上下文) |
| `click`              | `pos / preset / template`,`skip`,`delay`,`delay_preset` | 点一个坐标。三种模式见 [模板与预设系统](#模板与预设系统)                                            |
| `button`             | `name / template`,`skip`,`delay`,`delay_preset`         | 点等距按钮组里的某个,如 `chat_3`、`table_2`                                                         |
| `buy`                | `items: [(idx, qty), ...]`                              | 在购买界面循环点商品 + 数量 +1 + 确认                                                               |
| `sleep`              | `seconds / preset`                                      | 等待。preset 时引用 ClickDelays 字段(内置 16 个 + custom)                                           |
| `wait_pos_stable`    | `threshold`,`max_seconds`                               | OCR 读小地图坐标,等数字稳定不变(意味着移动完成 + 动画播完)                                          |
| `wait_screen_stable` | `threshold`,`max_seconds`                               | SSIM 比较截图,等画面稳定                                                                            |
| `include`            | `routine: str`                                          | 串联执行另一个 routine 文件,带防环检测                                                              |

---

## 模板与预设系统

这是项目最重要的设计抽象。简单说:**让用户在多个 routine 里复用相同的行为打包,改一处全局生效**。

### 三种预设系统

#### 1. 位置预设(UIPositions)

`movement_profile.ui_positions.custom` —— 一个 `dict[str, (x, y)]`。用户起任意名字,如 `张三丰`、`收购NPC`。在 ClickStep 编辑器的下拉里能直接选。

```yaml
ui_positions:
  custom:
    张三丰: [0.612, 0.483]
    收购NPC: [0.785, 0.612]
```

#### 2. 延时预设(ClickDelays)

`movement_profile.click_delays` —— 包含两类:

- **内置 16 个分类延时**(契约,不能删):`button`、`blank_skip`、`buy_item`、`travel_transition`、`fly`、`fly_settle` 等。内置流程(`_do_button` / `_do_buy` / `_do_travel`)硬读这些字段。
- **custom 字典**:用户起任意名字,如 `切场动画`、`等NPC说话完`。

任何 step 的 `delay` 字段都可以改用 `delay_preset` 引用上面的名字,运行时动态解析。删除 custom 后,引用断了的 step 运行时回退到 `default`。

#### 3. 行为模板(ClickTemplate / ButtonTemplate)

整体打包"位置 + skip + delay"作为一组。`ClickTemplate` 例子:

```yaml
click_templates:
  跳3次对话:
    position_preset: blank_btn   # 引用位置预设
    skip: 3
    delay_preset: 切场动画        # 也可引用延时预设
  退出后等2s:
    pos: [0.95, 0.88]            # 或字面坐标
    delay: 2.0
```

ClickStep 用 `template: 跳3次对话` 引用,这步的 pos/preset/skip/delay 全由模板填,改模板 → 所有引用它的 step 立即变。

### 为什么这样设计?

链式联动:

```
ClickStep.template = "跳3次对话"
        ↓
ClickTemplate { position_preset: "blank_btn", delay_preset: "切场动画", skip: 3 }
        ↓                                    ↓
UIPositions.blank_btn                 ClickDelays.travel_transition
[0.828, 0.957]                        3.0s
```

改任何一层,所有上层引用都自动更新。比改 yaml 文本搜索替换稳得多。

### 改名同步

模板 / custom 预设支持改名,改名时自动扫描 `config/routines/*.yaml`,把所有引用同步替换。每个被修改的 yaml 都会备份到 `.yaml.bak`。

---

## 项目结构

```
python/
├── main.py                       # 入口: QApplication + MainWindow
├── utils.py                      # Mumu / MumuInstall / MumuInfo: ADB + render_wnd 抽象
├── README.md                     # (本文档)
├── requirements.txt
│
├── app/
│   ├── core/                     # 业务逻辑层 (无 Qt 依赖, 可单独测试)
│   │   ├── profiles.py           # MovementConfig / UIPositions / ClickDelays /
│   │   │                         # ClickTemplate / ButtonTemplate / VisionSpec ...
│   │   ├── routine.py            # Routine + 11 种 Step 数据类
│   │   ├── runner.py             # RoutineRunner: 把 Step 分派到具体执行
│   │   ├── mover.py              # 走路引擎: 几何计算 + 相机贴边修正 + OCR 闭环
│   │   └── ocr.py                # TemplateOCR + CoordReader (小地图坐标)
│   │
│   └── views/                    # GUI 层 (PySide6, 每个对话框一个目录)
│       ├── main_window/          # 主菜单, 五个入口按钮
│       ├── movement_profile_dialog/  # 运动配置编辑器
│       ├── map_registry_dialog/  # 大地图地点编辑器
│       ├── routine_editor_dialog/    # routine yaml 可视化编辑器
│       ├── routine_runner_dialog/    # 执行器: 选 yaml + 启动 / 暂停 / 单步 / 停止
│       ├── position_picker/      # 取位置工具: 全局快捷键 + 放大截图
│       ├── click_preview_dialog/ # 点击位置截图预览 (调试用)
│       ├── roi_capture_dialog/   # ROI / OCR 字符模板截取工具
│       ├── debug_tools_dialog/   # 调试工具入口集合
│       ├── view_area_solver_dialog/  # 反解可视区域工具 (相机贴边参数)
│       └── map_size_solver_dialog/   # 反解 map_size 工具
│
├── config/
│   ├── common/                   # 跨 routine 的全局配置
│   │   ├── movement_profile.yaml
│   │   ├── map_switch_btn_position.yaml
│   │   └── map_registry.py       # MapRegistry / Profile / LocationRecord
│   ├── routines/                 # 用户 routine yaml
│   └── templates/
│       └── minimap_coord/        # OCR 字符模板 (0~9.png, lparen.png, ...)
│
├── debug/                        # 运行时输出 (截图、OCR 调试图)
└── tools/
    └── roi_captures/             # ROI 截取工具的输出
```

### 核心模块依赖

```
main.py
  └─ app.views.main_window
       ├─ utils.Mumu                         (ADB / 截图)
       ├─ app.views.routine_editor_dialog
       │   └─ app.core.routine               (数据)
       │   └─ app.core.profiles
       └─ app.views.routine_runner_dialog
           └─ app.core.runner                (调度器)
                ├─ app.core.routine
                ├─ app.core.profiles
                ├─ app.core.mover            (走路)
                │   └─ app.core.ocr
                └─ utils.Mumu
```

## 架构与运行时

### 数据分层(`utils.py`)

ADB / 模拟器交互被分成三层:

| 类            | 职责                                                            | 生命周期               |
| ------------- | --------------------------------------------------------------- | ---------------------- |
| `MumuInstall` | 磁盘安装位置(`adb.exe`、`MuMuManager.exe` 路径)                 | 一次性解析(注册表探测) |
| `MumuInfo`    | 运行时状态(`adb_port`、`render_wnd`、`is_android_started`)      | 启停时刷新             |
| `Mumu`        | 高层 API:`click(pos)`、`capture_window()`、`is_color_similar()` | 主对象                 |

### 执行模型(`runner.py`)

```
RoutineRunner.run()
  └─ for loop_idx in range(loop_count):
        for step in routine.steps:
            self._execute_one(step)              # 派发到 _do_<type>
            check_cancel() / check_pause()       # 每步之间检查
        sleep(loop_interval)                     # 每轮间隔
```

- **同步执行**,运行在调用线程
- GUI 用 `QThread` 包装,通过 `cancel_event` / `step_event` 控制
- 三种钩子:`on_log(level, msg)`、`on_progress(step, total, loop, loop_total)`、cancel/pause
- `IncludeStep` 串联另一份 routine 时带**栈追踪防环**

### Routine 编辑器架构

```
RoutineEditorWindow (主)
  ├─ 左:routine 文件列表 + 步骤列表 (按当前选中文件展开)
  ├─ 中:元数据 form (name / loop / starting_map)
  └─ 右:当前步骤的字段编辑器 (按 Step 类型动态构建)

子对话框 (按需弹出):
  ├─ _NewClickPresetDialog / _ManageClickPresetsDialog       (位置预设)
  ├─ _NewClickTemplateDialog / _ManageClickTemplatesDialog   (click 模板)
  ├─ _NewButtonTemplateDialog / _ManageButtonTemplatesDialog (button 模板)
  ├─ _NewClickDelayCustomDialog / _ManageClickDelaysCustomDialog  (custom 延时预设)
  └─ PositionPickerDialog                                    (从游戏取归一化坐标)

复合控件:
  └─ DelayInput                  (spinbox + 预设下拉 + 管理按钮, 在 5 处复用)
```

---

## 跨分辨率支持

项目对 1920×1080 / 2560×1440 / 全屏 / 窗口模式都通用,核心机制是:

1. **归一化坐标**:所有用户配置都是 `[0, 1]²`,运行时 `× device_w × device_h` 转 ADB tap
2. **`render_wnd` 客户区**:截图基于游戏渲染区(纯渲染,不含模拟器边框 / 标题栏 / 全屏模式的填充)
3. **取位置工具**:`PositionPicker` 全局快捷键,把鼠标在 `render_wnd` 客户区的位置直接转成归一化坐标

但有几个**不归一化**的地方,因为它们物理上跟分辨率挂钩:

- **大地图坐标**(`map_registry`):仍按分辨率分桶,因为 `bigmap_size_pixel` 是绝对像素,跟分辨率成正比
- **OCR 字符模板**:模板尺寸跟字体渲染像素挂钩,换分辨率要重新切

这两块在文档里有明确的"重新标定触发条件"。

### 2.5D 视角与相机贴边

游戏是 2.5D(从上方斜视),整个地图相当于顺时针旋转 45° 后渲染。地图 4 个角触碰屏幕边缘后,角色 sprite 不能再保持在 `character_pos`(屏幕中心),而是会沿屏幕边缘滑动。

`mover.py` 里的 `compute_character_screen_pos` 实现了这个修正:输入当前格子坐标 + 地图尺寸 + 视野档位 + `map_view_area`,几何推算角色实际渲染位置,进而推算出 click 走某格时应该点屏幕哪里。

`view_area_solver_dialog` 是配套的反解工具,从一组观测数据反推 `map_view_area`。

---

## OCR 闭环

走路完成的判定用 OCR 读小地图坐标实现。优于纯 SSIM 的原因:跑路途中"看起来稳定"但其实没到目标格(比如 NPC 走过画面)的误判会被坐标读数过滤掉。

### 管线

```
mumu.capture_window() → crop(minimap_coord_roi)
   → cv2.cvtColor(GRAY) → adaptiveThreshold(BINARY_INV)
   → 每个 glyph 模板做 cv2.matchTemplate(NCC)
   → x-center NMS 合并候选
   → 拼成字符串 → 正则解析为 (x, y)
```

字符模板放在 `config/templates/minimap_coord/`:`0.png` ~ `9.png` + `lparen.png` (`(`) + `rparen.png` (`)`) + `comma.png` (`,`)。

### 模板切割规范

参见模板目录的说明文件。要点:

1. **紧贴 bbox 切**,不留 padding(否则二值化后变噪点干扰 NCC)
2. **包含完整笔画**
3. **比例字体不补 padding**(字符 `1` 比其他窄,括号也比数字窄,各自切)
4. **保持原分辨率原色彩**(运行时是直接在原始截图尺寸上做匹配)

### 重新标定触发

- 游戏字体被官方改了
- 游戏分辨率换了(1080p ↔ 2K)
- 截图管线变了(PrintWindow 模式或 DPI 缩放)

普通玩家这些事件几乎不发生,模板一次切好基本永久可用。

### OCR 跑不通

启动 log 里看「OCR 闭环启用」是否打印;没打印说明模板加载失败或 ROI 未配置。运行 log 里搜 `OCR 阶段1超时未变化` / `OCR 阶段2超时未稳定` / `OCR 到达错位` 定位问题。

OCR 失败时会自动降级到 SSIM 兜底,routine 仍能运行,只是判定精度下降。

---

## 开发指南

### 运行测试

目前没有 pytest 测试套件。开发时手动跑 `python main.py` 验证。

`app/core/` 下的几个模块(`profiles.py`、`routine.py`)无 Qt 依赖,可以独立 import 跑数据层测试,例如:

```python
from app.core.profiles import MovementConfig, ClickTemplate
from app.core.routine import Routine, ClickStep

cfg = MovementConfig.load(Path("config/common/movement_profile.yaml"))
print(cfg.click_delays.resolve("travel_transition"))
```

### 加新的 Step 类型

1. 在 `app/core/routine.py` 加一个 `@dataclass` 继承 `Step`
2. 在 `step_from_dict` 里加 `type` 分支
3. 在 `app/core/runner.py` 加一个 `_do_<type>` 方法,在 `_execute_one` 的派发里加分支
4. 在 `app/views/routine_editor_dialog/window.py` 加 `_build_<type>_fields` UI 构建函数
5. 在保存校验 `_validate_routine` 里加字段检查

### 加新的 UI 元素位置

如果是单点(可被 ClickStep.preset 引用):

1. 在 `app/core/profiles.py` 的 `UIPositions` 加 `Optional[tuple[float, float]]` 字段
2. 在 `app/views/movement_profile_dialog/window.py` 的 `_build_entries` 加 `Entry(EntryKind.POINT, ...)`
3. 内置流程要用的话,在 `runner.py` 直接 `ui.<field>` 读

如果是用户自建的位置(不固定):用 routine 编辑器里的「+ 新建位置预设」,落在 `UIPositions.custom` 字典。

### 项目里的设计原则

读代码时会反复看到:

- **归一化优先**:任何对外 API 用 `(x, y) ∈ [0, 1]²`;像素只在 `Mumu` 类内部出现
- **数据 / 运行时 / UI 三层分离**:`profiles` 和 `routine` 是纯数据,`runner` 和 `mover` 是运行时,`views` 是 GUI;反向依赖禁止
- **配置而非代码**:阈值、延时、坐标都进 yaml,不硬编码到 .py
- **保留兜底兼容**:加新字段时老 yaml 必须能加载(默认值或迁移逻辑)
- **改一处全局生效**:模板和预设系统就是这个原则的体现

---

## 已知限制

- **只支持 MuMu 模拟器 + Windows**:`utils.py` 用注册表探测安装路径,用 `pywin32` 抓窗口句柄。换平台 / 换模拟器需要重写 `MumuInstall` / `MumuInfo`。
- **不防检测**:点击注入用 ADB tap,游戏端能识别(虽然概率低)。请遵守游戏服务条款,后果自负。
- **OCR 仅小地图坐标**:战斗 / 对话内容识别不在范围内。
- **打开多个 routine 编辑器同时改名预设会冲突**:改名扫描会写磁盘,但其他打开的编辑器内存里还是老引用,保存时覆盖。改名对话框关闭后会提示用户。
- **改名 yaml 重写格式漂移**:`safe_load` + `safe_dump` 会重写整个文件,可能改变格式(空格 / 引号 / 紧凑度)。每次都备份 `.yaml.bak`。
- **大地图配置仍按分辨率分桶**:换分辨率要重录地图坐标(因为 `bigmap_size_pixel` 跟分辨率挂钩)。

---

## 许可

见 LICENSE。本项目仅供学习和个人使用,不要用于商业用途、不要用于破坏游戏经济或扰乱其他玩家体验。