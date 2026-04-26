"""
Routine 编辑器主对话框。

布局:
  顶部       routine 选择条 (下拉 + 新建/复制/删除/重扫)
  上中部     元数据 form (name / description / loop_count / loop_interval / starting_map)
  中部       左：步骤列表  右：选中步骤的字段编辑器
  底部       重新加载 / 另存为 / 保存 / 关闭

设计要点:
  * step 数据双向绑定到字段 widget：spinbox/combo 等的 valueChanged/textChanged
    直接写回 step 对象，同时刷新左侧步骤摘要 + 标记 dirty。
  * 新增 step 走 _make_default_step：dataclass __post_init__ 校验空值，必须
    给占位字符串（"<选择目标>" 等），保存前校验把这些占位拦下来。
  * routine 切换 / dialog 关闭前若 dirty，弹 Save/Discard/Cancel。
  * MoveStep 编辑器的 OCR 按钮通过 self._ocr_current_pos 回调读取小地图坐标，
    CoordReader / TemplateOCR 在首次调用时构造并缓存。
"""

from __future__ import annotations

import logging
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Optional

import yaml

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from utils import Mumu

from app.core.ocr import CoordReader, TemplateOCR
from app.core.profiles import (
    DEFAULT_MOVEMENT_YAML_PATH,
    ButtonTemplate,
    ClickTemplate,
    MovementConfig,
)
from app.core.routine import (
    AnyStep,
    ButtonStep,
    BuyStep,
    CLICK_PRESETS,
    CLICK_PRESET_NAMES,
    ClickStep,
    DEFAULT_ROUTINES_DIR,
    EnterMapStep,
    IncludeStep,
    MoveStep,
    Routine,
    SLEEP_PRESETS,
    SLEEP_PRESET_NAMES,
    SleepStep,
    TravelStep,
    WaitPosStableStep,
    WaitScreenStableStep,
    list_routines,
)
from app.views.position_picker import PositionPickerDialog
from app.views.routine_editor_dialog.widgets import BuyItemsWidget, PathListWidget
from config.common.map_registry import (
    DEFAULT_YAML_PATH as MAP_REGISTRY_PATH,
    MapRegistry,
)

log = logging.getLogger(__name__)


# 字符模板目录（与 roi_capture_dialog 的 CHAR_OUTPUT_DIR 保持一致）
MINIMAP_TEMPLATE_DIR = Path("config/templates/minimap_coord")


# 7 种"常用"step + 3 种"高级"step
COMMON_STEP_TYPES = [
    ("travel", "travel — 大地图传送"),
    ("move", "move — 当前地图内走路径"),
    ("button", "button — 场景/对话按钮"),
    ("click", "click — 归一化坐标点击"),
    ("buy", "buy — 购买商品"),
    ("sleep", "sleep — 等待秒数"),
    ("include", "include — 串联执行另一个 routine"),
]

ADVANCED_STEP_TYPES = [
    ("wait_pos_stable", "wait_pos_stable — 等小地图坐标稳定"),
    ("wait_screen_stable", "wait_screen_stable — 等画面稳定"),
    ("enter_map", "enter_map — 标记切换到某地图"),
]


def _make_default_step(type_str: str) -> AnyStep:
    """
    创建 step 实例。dataclass __post_init__ 对必填字段做非空校验，
    必须给一个临时占位（"<选择目标>" / [(0,0)] / ...），保存前会校验。
    """
    if type_str == "travel":
        return TravelStep(to="<选择目标>")
    if type_str == "move":
        return MoveStep(path=[(0, 0)])
    if type_str == "button":
        return ButtonStep(name="chat_1")
    if type_str == "click":
        return ClickStep(pos=(0.5, 0.5))
    if type_str == "buy":
        return BuyStep(items=[(1, 1)])
    if type_str == "sleep":
        return SleepStep(seconds=1.0)
    if type_str == "include":
        return IncludeStep(routine="<选择 routine>")
    if type_str == "wait_pos_stable":
        return WaitPosStableStep()
    if type_str == "wait_screen_stable":
        return WaitScreenStableStep()
    if type_str == "enter_map":
        return EnterMapStep(map="<选择地图>")
    raise ValueError(f"未知 step 类型: {type_str}")


def _describe_step(s: AnyStep) -> str:
    """步骤列表里的一行摘要"""
    t = s.TYPE
    at = f" @{s.at_map}" if s.at_map else ""
    if t == "travel":
        return f"travel → {s.to}{at}"
    if t == "move":
        p = s.path
        if not p:
            return f"move (空路径!){at}"
        if len(p) <= 3:
            return f"move {p}{at}"
        return f"move {p[0]} → ... → {p[-1]} ({len(p)} 点){at}"
    if t == "button":
        if s.template:
            return f"button ★[{s.template}]{at}"
        skip = f" skip={s.skip}" if s.skip else ""
        if s.delay_preset:
            delay = f" delay◆{s.delay_preset}"
        elif s.delay:
            delay = f" delay={s.delay}"
        else:
            delay = ""
        return f"button {s.name}{skip}{delay}{at}"
    if t == "click":
        if s.template:
            return f"click ★[{s.template}]{at}"
        if s.preset:
            head = f"click [{s.preset}]"
        else:
            head = f"click ({s.pos[0]:.3f}, {s.pos[1]:.3f})"
        skip = f" skip={s.skip}" if s.skip else ""
        if s.delay_preset:
            delay = f" delay◆{s.delay_preset}"
        elif s.delay:
            delay = f" delay={s.delay}"
        else:
            delay = ""
        return f"{head}{skip}{delay}{at}"
    if t == "buy":
        head = ", ".join(f"({i},{q})" for i, q in s.items[:3])
        more = "..." if len(s.items) > 3 else ""
        return f"buy [{head}{more}] ({len(s.items)} 项){at}"
    if t == "sleep":
        if s.preset:
            return f"sleep [{s.preset}]{at}"
        return f"sleep {s.seconds}s{at}"
    if t == "include":
        return f"include → {s.routine}{at}"
    if t == "wait_pos_stable":
        return f"wait_pos_stable max_wait={s.max_wait}s{at}"
    if t == "wait_screen_stable":
        return f"wait_screen_stable max_wait={s.max_wait}s{at}"
    if t == "enter_map":
        return f"enter_map → {s.map}{at}"
    return t


class RoutineEditorDialog(QDialog):
    def __init__(
        self,
        mumu: Mumu,
        parent=None,
        routines_dir: Path = DEFAULT_ROUTINES_DIR,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Routine 编辑器")
        self.resize(1100, 740)

        self._mumu = mumu
        self._routines_dir = routines_dir
        self._routine: Optional[Routine] = None
        self._dirty = False
        self._suspend_dirty = False  # 程序性更新 form 时关闭 dirty 跟踪

        # 加载副配置（地图列表 + 按钮组数量 + movement_profile 引用）
        # 地图清单分两份:
        #   _known_map_locations:    全部地图名 (不过滤). 所有地图下拉
        #                            (travel / at_map / enter_map / starting_map)
        #                            都用这份, 让用户能先写 routine 后录入地图信息.
        #   _recorded_map_locations: 录了大地图位置 (icon + btn 像素) 的子集.
        #                            目前 GUI 不直接引用, 保留供未来在下拉里加
        #                            "未录入" 标记或导出过滤之类的用途.
        self._recorded_map_locations: list[str] = []
        self._known_map_locations: list[str] = []
        # 旧别名: 保留指向 _known_map_locations 兼容外部可能的引用 (内部不再使用)
        self._map_locations: list[str] = []
        self._chat_btn_count: int = 6
        self._table_btn_count: int = 6
        self._movement_profile: Optional[MovementConfig] = None
        self._load_side_data()

        # OCR 链路 lazy 缓存（首次按 OCR 按钮时构造）
        self._template_ocr: Optional[TemplateOCR] = None
        self._coord_reader: Optional[CoordReader] = None

        self._build_ui()
        self._reload_routines_combo()

    # ========================================================================
    # 副数据加载
    # ========================================================================

    def _load_side_data(self) -> None:
        """读 map_registry 拿地图列表，读 movement_profile 拿 chat/table 按钮数 + ROI"""
        try:
            map_reg = MapRegistry.load(MAP_REGISTRY_PATH)
            key = f"{self._mumu.device_w}x{self._mumu.device_h}"
            prof = map_reg.profiles.get(key)
            if prof:
                # 录了大地图位置的: 用于 travel.to (作为"已可用"提示, 但 travel 下拉
                # 实际仍用 _known_map_locations 列全部地图, 保持"先写 config 后录入"
                # 的工作流). 该字段当前未在 GUI 引用, 保留作未来"未录入标记"用途.
                self._recorded_map_locations = sorted(
                    name for name, rec in prof.locations.items() if rec.is_recorded
                )
                # 全部地图名, 不做任何过滤. 让用户在 routine 里能引用尚未录入的地图
                # (先写 routine 后补 map_registry 信息这种工作流). 引用了未录入的
                # 地图时, 跑 routine 会在 runner._make_map_ctx / _do_travel 报错.
                self._known_map_locations = sorted(prof.locations.keys())
                # 旧别名兼容
                self._map_locations = self._known_map_locations
            else:
                log.warning(
                    "map_registry 没有分辨率 %s 的 profile，地图下拉将为空", key
                )
        except Exception as e:
            log.warning("加载 map_registry 失败: %s", e)

        try:
            mp = MovementConfig.load(DEFAULT_MOVEMENT_YAML_PATH)
            self._movement_profile = mp  # 缓存供 OCR 使用
            ui = mp.ui
            # 兼容新旧 schema：
            #   旧: chat_btn_pos_list (list[tuple])
            #   新: chat_btn_group (LinearButtonGroup, 有 count 字段)
            if (
                hasattr(ui, "chat_btn_group")
                and getattr(ui, "chat_btn_group") is not None
            ):
                self._chat_btn_count = ui.chat_btn_group.count
            elif hasattr(ui, "chat_btn_pos_list"):
                self._chat_btn_count = max(6, len(ui.chat_btn_pos_list))

            if (
                hasattr(ui, "table_btn_group")
                and getattr(ui, "table_btn_group") is not None
            ):
                self._table_btn_count = ui.table_btn_group.count
            elif hasattr(ui, "table_btn_pos_list"):
                self._table_btn_count = max(6, len(ui.table_btn_pos_list))
        except Exception as e:
            log.warning("加载 movement_profile 失败: %s", e)

    # ========================================================================
    # OCR 链路
    # ========================================================================

    def _get_coord_reader(self) -> CoordReader:
        """
        Lazy 创建 CoordReader（首次调用构造并缓存）。
        失败抛 RuntimeError，message 直接给用户看。
        """
        if self._coord_reader is not None:
            return self._coord_reader

        if self._movement_profile is None:
            raise RuntimeError(
                f"未配置当前分辨率（{self._mumu.device_w}×{self._mumu.device_h}）"
                f"的运动配置。请先在主界面「运动配置」里录入。"
            )

        roi = self._movement_profile.minimap_coord_roi
        if roi is None:
            raise RuntimeError(
                "运动配置里未录入 minimap_coord_roi（小地图坐标 ROI）。"
                "请先用主界面「ROI 截取工具」录入；该工具会自动同步到运动配置。"
            )

        if self._template_ocr is None:
            try:
                self._template_ocr = TemplateOCR.from_dir(MINIMAP_TEMPLATE_DIR)
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"OCR 字符模板目录无可用模板: {MINIMAP_TEMPLATE_DIR}\n"
                    f"原因: {e}\n"
                    f"请先用主界面「ROI 截取工具」录入字符模板（0~9 + ( ) ,）。"
                ) from e
            except Exception as e:
                raise RuntimeError(f"OCR 模板加载失败: {type(e).__name__}: {e}") from e

        self._coord_reader = CoordReader(
            mumu=self._mumu,
            ocr=self._template_ocr,
            roi_norm=roi,
        )
        log.info(
            "OCR 链路就绪: roi=%s, template_dir=%s",
            roi,
            MINIMAP_TEMPLATE_DIR,
        )
        return self._coord_reader

    def _ocr_current_pos(self) -> tuple[int, int]:
        """
        OCR 读取小地图当前坐标，作为 PathListWidget 的回调。
        失败抛 RuntimeError（widget 会显示 message 给用户）。
        """
        reader = self._get_coord_reader()
        try:
            coord, raw_text, _roi = reader.read_verbose()
        except Exception as e:
            # 把截图 / OCR 内部的异常包一层带上下文
            raise RuntimeError(f"OCR 调用异常: {type(e).__name__}: {e}") from e

        if coord is None:
            raise RuntimeError(
                f"OCR 读不到坐标。\n"
                f"OCR 拼出的原始字符串: {raw_text!r}\n\n"
                f"可能原因:\n"
                f"  · 游戏小地图当前未显示坐标数字\n"
                f"  · ROI 框错位置（minimap_coord_roi 需要重录）\n"
                f"  · 字符模板需要重新校准（用「ROI 截取工具」重切相关字符）"
            )
        log.info("OCR 取得坐标: %s (raw=%r)", coord, raw_text)
        return coord

    # ========================================================================
    # UI 构造
    # ========================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── 顶部：routine 选择条 ──
        top = QHBoxLayout()
        top.addWidget(QLabel("Routine:"))
        self._routine_combo = QComboBox()
        self._routine_combo.setMinimumWidth(220)
        self._routine_combo.currentIndexChanged.connect(self._on_routine_combo_changed)
        top.addWidget(self._routine_combo)

        btn_reload_combo = QPushButton("↻")
        btn_reload_combo.setMaximumWidth(36)
        btn_reload_combo.setToolTip("重新扫描 routines 目录")
        btn_reload_combo.clicked.connect(self._reload_routines_combo)
        top.addWidget(btn_reload_combo)

        btn_new = QPushButton("新建")
        btn_new.clicked.connect(self._on_new_routine)
        top.addWidget(btn_new)

        btn_copy = QPushButton("复制")
        btn_copy.clicked.connect(self._on_copy_routine)
        top.addWidget(btn_copy)

        btn_delete = QPushButton("删除")
        btn_delete.clicked.connect(self._on_delete_routine)
        top.addWidget(btn_delete)

        top.addStretch(1)
        root.addLayout(top)

        # ── 元数据 form ──
        meta_form = QFormLayout()
        meta_form.setContentsMargins(0, 0, 0, 0)

        self._name_edit = QLineEdit()
        self._name_edit.textChanged.connect(self._on_meta_changed)
        meta_form.addRow("name:", self._name_edit)

        self._desc_edit = QLineEdit()
        self._desc_edit.textChanged.connect(self._on_meta_changed)
        meta_form.addRow("description:", self._desc_edit)

        loop_row = QHBoxLayout()
        self._loop_count = QSpinBox()
        self._loop_count.setRange(0, 999999)
        self._loop_count.setSpecialValueText("∞ (无限)")
        self._loop_count.valueChanged.connect(self._on_meta_changed)
        loop_row.addWidget(self._loop_count)
        loop_row.addWidget(QLabel("每轮间隔(秒):"))
        self._loop_interval = DelayInput(
            click_delays_provider=lambda: (
                self._movement_profile.click_delays
                if self._movement_profile is not None
                else None
            ),
            manage_callback=self._open_manage_click_delays_custom_dialog,
            parent=self,
            max_seconds=600.0,
        )
        self._loop_interval.changed.connect(self._on_meta_changed)
        loop_row.addWidget(self._loop_interval, 1)
        meta_form.addRow("loop_count:", _wrap(loop_row))

        self._starting_map_combo = QComboBox()
        self._starting_map_combo.currentIndexChanged.connect(self._on_meta_changed)
        meta_form.addRow("starting_map:", self._starting_map_combo)

        meta_w = QWidget()
        meta_w.setLayout(meta_form)
        root.addWidget(meta_w)

        root.addWidget(_hline())

        # ── 中部：步骤列表 + 编辑器 ──
        middle = QHBoxLayout()

        # 左：步骤列表
        left = QVBoxLayout()
        left.addWidget(QLabel("步骤"))
        self._steps_list = QListWidget()
        self._steps_list.currentRowChanged.connect(self._on_step_sel_changed)
        left.addWidget(self._steps_list, 1)

        steps_btns = QHBoxLayout()
        self._btn_add_step = QPushButton("+ 添加")
        self._btn_add_step.clicked.connect(self._on_add_step)
        steps_btns.addWidget(self._btn_add_step)
        btn_up = QPushButton("↑")
        btn_up.setMaximumWidth(36)
        btn_up.clicked.connect(self._on_step_up)
        steps_btns.addWidget(btn_up)
        btn_down = QPushButton("↓")
        btn_down.setMaximumWidth(36)
        btn_down.clicked.connect(self._on_step_down)
        steps_btns.addWidget(btn_down)
        btn_dup = QPushButton("⊕")
        btn_dup.setMaximumWidth(36)
        btn_dup.setToolTip("复制选中步骤")
        btn_dup.clicked.connect(self._on_step_duplicate)
        steps_btns.addWidget(btn_dup)
        btn_del = QPushButton("✖")
        btn_del.setMaximumWidth(36)
        btn_del.setToolTip("删除选中步骤")
        btn_del.clicked.connect(self._on_step_delete)
        steps_btns.addWidget(btn_del)
        left.addLayout(steps_btns)

        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setMinimumWidth(380)
        left_w.setMaximumWidth(460)
        middle.addWidget(left_w)

        # 右：编辑器（包在 ScrollArea 里防止字段超长）
        self._editor_scroll = QScrollArea()
        self._editor_scroll.setWidgetResizable(True)
        self._editor_host = QWidget()
        self._editor_host_layout = QVBoxLayout(self._editor_host)
        self._editor_host_layout.setContentsMargins(8, 0, 0, 0)
        self._clear_editor()
        self._editor_scroll.setWidget(self._editor_host)
        middle.addWidget(self._editor_scroll, 1)

        root.addLayout(middle, 1)

        # ── 底部 ──
        bottom = QHBoxLayout()
        btn_reload = QPushButton("重新加载")
        btn_reload.clicked.connect(self._on_reload_current)
        bottom.addWidget(btn_reload)
        bottom.addStretch(1)

        btn_save_as = QPushButton("另存为...")
        btn_save_as.clicked.connect(self._on_save_as)
        bottom.addWidget(btn_save_as)

        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._on_save)
        bottom.addWidget(btn_save)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        bottom.addWidget(btn_close)
        root.addLayout(bottom)

    # ========================================================================
    # 地图下拉填充 helper
    # ========================================================================

    def _populate_map_combo(
        self,
        combo: QComboBox,
        current: Optional[str],
        with_empty: bool,
    ) -> None:
        """
        填充地图下拉 (用于 at_map / starting_map). current 不在
        self._known_map_locations 里也会被加进去 (标 *未录入*), 保证 round-trip 不丢值.
        """
        combo.blockSignals(True)
        combo.clear()
        if with_empty:
            combo.addItem("(不指定)", None)
        names = list(self._known_map_locations)
        if current and current not in names and not str(current).startswith("<"):
            names.append(current)
            extra_marker = current
        else:
            extra_marker = None
        for name in names:
            label = name if name != extra_marker else f"{name} (未录入)"
            combo.addItem(label, name)

        # 选中
        if current is None:
            combo.setCurrentIndex(0 if with_empty else -1)
        else:
            i = combo.findData(current)
            combo.setCurrentIndex(i if i >= 0 else (0 if with_empty else -1))
        combo.blockSignals(False)

    # ========================================================================
    # routine 文件管理
    # ========================================================================

    def _reload_routines_combo(self) -> None:
        prev = self._routine_combo.currentData()
        self._routine_combo.blockSignals(True)
        self._routine_combo.clear()
        for p in list_routines(self._routines_dir):
            self._routine_combo.addItem(p.name, p)
        self._routine_combo.blockSignals(False)

        # 恢复
        if prev is not None:
            i = self._routine_combo.findData(prev)
            if i >= 0:
                self._routine_combo.setCurrentIndex(i)
                return
        if self._routine_combo.count() > 0:
            self._routine_combo.setCurrentIndex(0)
            self._on_routine_combo_changed(0)
        else:
            self._routine = None
            self._refresh_meta_form()
            self._refresh_steps_list()

    def _on_routine_combo_changed(self, _idx: int) -> None:
        new_path = self._routine_combo.currentData()
        # 同一文件不重新加载
        if (
            self._routine is not None
            and self._routine.path is not None
            and new_path == self._routine.path
        ):
            return
        if not self._maybe_save_dirty():
            # 用户取消，恢复 combo 到旧 routine
            self._routine_combo.blockSignals(True)
            if self._routine and self._routine.path:
                i = self._routine_combo.findData(self._routine.path)
                self._routine_combo.setCurrentIndex(i if i >= 0 else 0)
            else:
                self._routine_combo.setCurrentIndex(-1)
            self._routine_combo.blockSignals(False)
            return
        if new_path is None:
            self._routine = None
            self._refresh_meta_form()
            self._refresh_steps_list()
            self._update_title()
        else:
            self._load_routine_from(new_path)

    def _load_routine_from(self, path: Path) -> None:
        try:
            self._routine = Routine.load(path)
        except Exception as e:
            log.exception("加载 routine 失败")
            QMessageBox.critical(self, "加载失败", f"{type(e).__name__}: {e}")
            self._routine = None
            return
        self._dirty = False
        self._refresh_meta_form()
        self._refresh_steps_list()
        self._update_title()

    def _on_new_routine(self) -> None:
        if not self._maybe_save_dirty():
            return
        name, ok = QInputDialog.getText(self, "新建 Routine", "文件名（不含扩展名）:")
        name = (name or "").strip()
        if not ok or not name:
            return
        target = self._routines_dir / f"{name}.yaml"
        if target.exists():
            QMessageBox.warning(self, "已存在", f"{target.name} 已存在")
            return
        new_routine = Routine(name=name, steps=[])
        try:
            new_routine.save(target)
        except Exception as e:
            QMessageBox.critical(self, "新建失败", f"{type(e).__name__}: {e}")
            return
        self._reload_routines_combo()
        i = self._routine_combo.findData(target)
        if i >= 0:
            self._routine_combo.setCurrentIndex(i)

    def _on_copy_routine(self) -> None:
        if self._routine is None or self._routine.path is None:
            QMessageBox.information(self, "提示", "请先选择一个 routine")
            return
        if not self._maybe_save_dirty():
            return
        suggested = f"{self._routine.name}_copy"
        name, ok = QInputDialog.getText(
            self, "复制 Routine", "新文件名（不含扩展名）:", text=suggested
        )
        name = (name or "").strip()
        if not ok or not name:
            return
        target = self._routines_dir / f"{name}.yaml"
        if target.exists():
            QMessageBox.warning(self, "已存在", f"{target.name} 已存在")
            return
        try:
            shutil.copyfile(self._routine.path, target)
        except Exception as e:
            QMessageBox.critical(self, "复制失败", f"{type(e).__name__}: {e}")
            return
        self._reload_routines_combo()
        i = self._routine_combo.findData(target)
        if i >= 0:
            self._routine_combo.setCurrentIndex(i)

    def _on_delete_routine(self) -> None:
        if self._routine is None or self._routine.path is None:
            return
        ans = QMessageBox.question(
            self,
            "确认删除",
            f"删除文件 {self._routine.path.name}？该操作不可撤销。",
        )
        if ans != QMessageBox.Yes:
            return
        try:
            self._routine.path.unlink()
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"{type(e).__name__}: {e}")
            return
        self._routine = None
        self._dirty = False
        self._reload_routines_combo()

    def _on_save(self) -> None:
        if self._routine is None:
            return
        self._flush_meta_to_routine()
        err = self._validate_routine()
        if err:
            QMessageBox.warning(self, "无法保存", err)
            return
        try:
            self._routine.save()
        except Exception as e:
            log.exception("保存 routine 失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return
        self._dirty = False
        self._update_title()
        QMessageBox.information(self, "已保存", f"已写入: {self._routine.path}")

    def _on_save_as(self) -> None:
        if self._routine is None:
            return
        self._flush_meta_to_routine()
        err = self._validate_routine()
        if err:
            QMessageBox.warning(self, "无法保存", err)
            return
        fn, _ = QFileDialog.getSaveFileName(
            self,
            "另存 Routine 为...",
            str(self._routines_dir),
            "YAML (*.yaml *.yml)",
        )
        if not fn:
            return
        try:
            self._routine.save(fn)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return
        self._dirty = False
        self._reload_routines_combo()
        self._update_title()

    def _on_reload_current(self) -> None:
        if self._routine is None or self._routine.path is None:
            return
        if self._dirty:
            ans = QMessageBox.question(
                self, "重新加载", "丢弃当前改动，从磁盘重新加载？"
            )
            if ans != QMessageBox.Yes:
                return
        self._load_routine_from(self._routine.path)

    def _maybe_save_dirty(self) -> bool:
        """切换/关闭前检查 dirty。返回 False = 用户取消"""
        if not self._dirty or self._routine is None:
            return True
        ans = QMessageBox.question(
            self,
            "未保存",
            f"「{self._routine.name}」有未保存的改动，是否保存？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if ans == QMessageBox.Cancel:
            return False
        if ans == QMessageBox.Save:
            self._on_save()
            return not self._dirty  # 保存失败仍 dirty
        # Discard
        self._dirty = False
        return True

    def _validate_routine(self) -> Optional[str]:
        """保存前校验。返回错误描述或 None。"""
        if self._routine is None:
            return "未加载 routine"
        if not self._routine.name:
            return "name 不能为空"
        if self._routine.loop_interval_preset and not self._is_delay_preset_valid(
            self._routine.loop_interval_preset
        ):
            return (
                f"loop_interval_preset {self._routine.loop_interval_preset!r} "
                f"在当前 ClickDelays 里不存在 (内置 + custom)"
            )
        cur_stem = self._routine.path.stem if self._routine.path is not None else None
        for i, s in enumerate(self._routine.steps, start=1):
            if isinstance(s, TravelStep):
                if not s.to or s.to.startswith("<"):
                    return f"第 {i} 步 (travel): to 未填"
            elif isinstance(s, EnterMapStep):
                if not s.map or s.map.startswith("<"):
                    return f"第 {i} 步 (enter_map): map 未填"
            elif isinstance(s, ButtonStep):
                if s.template:
                    if (
                        self._movement_profile is None
                        or s.template not in self._movement_profile.button_templates
                    ):
                        return (
                            f"第 {i} 步 (button): button 模板 {s.template!r} "
                            f"在当前运动配置里不存在; 请去 routine 编辑器"
                            f"「新建 button 模板」录入或换用其他选项"
                        )
                else:
                    if not s.name or s.name.startswith("<"):
                        return f"第 {i} 步 (button): name 未填"
                    if "_" not in s.name:
                        return (
                            f"第 {i} 步 (button): name={s.name!r} "
                            f"应为 chat_N / table_N 形式"
                        )
                # delay_preset 校验
                if s.delay_preset and not self._is_delay_preset_valid(s.delay_preset):
                    return (
                        f"第 {i} 步 (button): delay_preset {s.delay_preset!r} "
                        f"在当前 ClickDelays 里不存在 (内置 + custom)"
                    )
            elif isinstance(s, MoveStep) and not s.path:
                return f"第 {i} 步 (move): path 不能为空"
            elif isinstance(s, ClickStep):
                if s.preset and s.template:
                    return (
                        f"第 {i} 步 (click): preset 和 template 互斥, " f"不能同时设置"
                    )
                if s.template:
                    if (
                        self._movement_profile is None
                        or s.template not in self._movement_profile.click_templates
                    ):
                        return (
                            f"第 {i} 步 (click): click 模板 {s.template!r} "
                            f"在当前运动配置里不存在; 请去 routine 编辑器"
                            f"「新建 click 模板」录入或换用其他选项"
                        )
                elif s.preset:
                    valid = set(CLICK_PRESET_NAMES) | {"character_pos"}
                    if self._movement_profile is not None:
                        valid.update(self._movement_profile.ui.custom.keys())
                    if s.preset not in valid:
                        return (
                            f"第 {i} 步 (click): preset {s.preset!r} 非法；"
                            f"合法值: 内置 {sorted(CLICK_PRESET_NAMES)} + "
                            f"character_pos + 当前自建预设"
                        )
                if s.delay_preset and not self._is_delay_preset_valid(s.delay_preset):
                    return (
                        f"第 {i} 步 (click): delay_preset {s.delay_preset!r} "
                        f"在当前 ClickDelays 里不存在 (内置 + custom)"
                    )
            elif isinstance(s, SleepStep):
                if s.preset and not self._is_delay_preset_valid(s.preset):
                    return (
                        f"第 {i} 步 (sleep): preset {s.preset!r} 在当前 "
                        f"ClickDelays 里不存在; 合法值: 内置 16 个 + custom"
                    )
            elif isinstance(s, BuyStep) and not s.items:
                return f"第 {i} 步 (buy): items 不能为空"
            elif isinstance(s, IncludeStep):
                if not s.routine or s.routine.startswith("<"):
                    return f"第 {i} 步 (include): routine 未填"
                # 直接自引用拦截 (间接环由 runner 在执行时检测)
                if cur_stem and s.routine == cur_stem:
                    return (
                        f"第 {i} 步 (include): 不能引用 routine 自己 " f"({s.routine})"
                    )
        return None

    # ========================================================================
    # 元数据 / 步骤列表 同步
    # ========================================================================

    def _refresh_meta_form(self) -> None:
        self._suspend_dirty = True
        try:
            r = self._routine
            self._name_edit.setText(r.name if r else "")
            self._desc_edit.setText(r.description if r else "")
            self._loop_count.setValue(r.loop_count if r else 1)
            self._loop_interval.set_value(
                r.loop_interval if r else 0.0,
                r.loop_interval_preset if r else None,
            )
            self._populate_map_combo(
                self._starting_map_combo,
                r.starting_map if r else None,
                with_empty=True,
            )
        finally:
            self._suspend_dirty = False

    def _refresh_steps_list(self) -> None:
        prev_row = self._steps_list.currentRow()
        self._steps_list.blockSignals(True)
        self._steps_list.clear()
        if self._routine:
            for i, s in enumerate(self._routine.steps, start=1):
                self._steps_list.addItem(
                    QListWidgetItem(f"{i:>3}. {_describe_step(s)}")
                )
        self._steps_list.blockSignals(False)

        if 0 <= prev_row < self._steps_list.count():
            self._steps_list.setCurrentRow(prev_row)
        elif self._steps_list.count() > 0:
            self._steps_list.setCurrentRow(0)
        else:
            self._clear_editor()

    def _flush_meta_to_routine(self) -> None:
        if self._routine is None:
            return
        new_name = self._name_edit.text().strip()
        if new_name:
            self._routine.name = new_name
        self._routine.description = self._desc_edit.text().strip()
        self._routine.loop_count = self._loop_count.value()
        seconds, preset = self._loop_interval.value()
        self._routine.loop_interval = seconds
        self._routine.loop_interval_preset = preset
        self._routine.starting_map = self._starting_map_combo.currentData()

    def _on_meta_changed(self, *_args) -> None:
        if self._suspend_dirty or self._routine is None:
            return
        self._dirty = True
        self._update_title()

    def _mark_dirty(self) -> None:
        if self._suspend_dirty or self._routine is None:
            return
        self._dirty = True
        self._update_title()

    def _update_title(self) -> None:
        base = "Routine 编辑器"
        if self._routine is not None:
            base = f"{base} — {self._routine.name}"
            if self._dirty:
                base += " *"
        self.setWindowTitle(base)

    # ========================================================================
    # 步骤列表操作
    # ========================================================================

    def _on_step_sel_changed(self, row: int) -> None:
        if self._routine is None or row < 0 or row >= len(self._routine.steps):
            self._clear_editor()
            return
        self._build_editor_for(self._routine.steps[row], row)

    def _on_add_step(self) -> None:
        if self._routine is None:
            QMessageBox.information(self, "提示", "请先选择或新建一个 routine")
            return
        menu = QMenu(self)
        for type_str, label in COMMON_STEP_TYPES:
            act = QAction(label, self)
            act.triggered.connect(
                lambda _checked=False, t=type_str: self._do_add_step(t)
            )
            menu.addAction(act)
        adv = menu.addMenu("▸ 高级")
        for type_str, label in ADVANCED_STEP_TYPES:
            act = QAction(label, self)
            act.triggered.connect(
                lambda _checked=False, t=type_str: self._do_add_step(t)
            )
            adv.addAction(act)
        sender = self.sender()
        if hasattr(sender, "mapToGlobal") and hasattr(sender, "rect"):
            menu.exec(sender.mapToGlobal(sender.rect().bottomLeft()))
        else:
            menu.exec()

    def _do_add_step(self, type_str: str) -> None:
        if self._routine is None:
            return
        new_step = _make_default_step(type_str)
        cur = self._steps_list.currentRow()
        insert_at = (cur + 1) if cur >= 0 else len(self._routine.steps)
        self._routine.steps.insert(insert_at, new_step)
        self._refresh_steps_list()
        self._steps_list.setCurrentRow(insert_at)
        self._mark_dirty()

    def _on_step_up(self) -> None:
        if self._routine is None:
            return
        row = self._steps_list.currentRow()
        if row <= 0:
            return
        self._routine.steps[row], self._routine.steps[row - 1] = (
            self._routine.steps[row - 1],
            self._routine.steps[row],
        )
        self._refresh_steps_list()
        self._steps_list.setCurrentRow(row - 1)
        self._mark_dirty()

    def _on_step_down(self) -> None:
        if self._routine is None:
            return
        row = self._steps_list.currentRow()
        if row < 0 or row >= len(self._routine.steps) - 1:
            return
        self._routine.steps[row], self._routine.steps[row + 1] = (
            self._routine.steps[row + 1],
            self._routine.steps[row],
        )
        self._refresh_steps_list()
        self._steps_list.setCurrentRow(row + 1)
        self._mark_dirty()

    def _on_step_duplicate(self) -> None:
        if self._routine is None:
            return
        row = self._steps_list.currentRow()
        if row < 0:
            return
        clone = deepcopy(self._routine.steps[row])
        self._routine.steps.insert(row + 1, clone)
        self._refresh_steps_list()
        self._steps_list.setCurrentRow(row + 1)
        self._mark_dirty()

    def _on_step_delete(self) -> None:
        if self._routine is None:
            return
        row = self._steps_list.currentRow()
        if row < 0:
            return
        ans = QMessageBox.question(self, "确认", f"删除第 {row + 1} 步？")
        if ans != QMessageBox.Yes:
            return
        del self._routine.steps[row]
        self._refresh_steps_list()
        self._mark_dirty()

    # ========================================================================
    # 步骤编辑器
    # ========================================================================

    def _clear_editor(self) -> None:
        while self._editor_host_layout.count():
            item = self._editor_host_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        ph = QLabel("请在左侧选一个步骤")
        ph.setAlignment(Qt.AlignCenter)
        self._editor_host_layout.addWidget(ph)
        self._editor_host_layout.addStretch(1)

    def _build_editor_for(self, step: AnyStep, row: int) -> None:
        # 清空
        while self._editor_host_layout.count():
            item = self._editor_host_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        editor = self._make_step_editor(step, row)
        self._editor_host_layout.addWidget(editor)
        self._editor_host_layout.addStretch(1)

    def _make_step_editor(self, step: AnyStep, row: int) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(0, 0, 0, 0)

        title = QLabel(f"<b>第 {row + 1} 步: {step.TYPE}</b>")
        form.addRow(title, QLabel(""))

        # 通用 at_map
        at_combo = QComboBox()
        self._populate_map_combo(at_combo, step.at_map, with_empty=True)

        def _on_at_changed(_i: int) -> None:
            step.at_map = at_combo.currentData()
            self._refresh_step_label(row)
            self._mark_dirty()

        at_combo.currentIndexChanged.connect(_on_at_changed)
        form.addRow("at_map:", at_combo)

        # 类型特有字段
        if isinstance(step, TravelStep):
            self._build_travel_fields(form, step, row)
        elif isinstance(step, MoveStep):
            self._build_move_fields(form, step, row)
        elif isinstance(step, ButtonStep):
            self._build_button_fields(form, step, row)
        elif isinstance(step, ClickStep):
            self._build_click_fields(form, step, row)
        elif isinstance(step, BuyStep):
            self._build_buy_fields(form, step, row)
        elif isinstance(step, SleepStep):
            self._build_sleep_fields(form, step, row)
        elif isinstance(step, IncludeStep):
            self._build_include_fields(form, step, row)
        elif isinstance(step, WaitPosStableStep):
            self._build_wait_common_fields(form, step, row)
        elif isinstance(step, WaitScreenStableStep):
            self._build_wait_common_fields(form, step, row)
        elif isinstance(step, EnterMapStep):
            self._build_enter_map_fields(form, step, row)
        else:
            form.addRow(QLabel(f"<i>未知步骤类型: {step.TYPE}</i>"), QLabel(""))

        return w

    def _refresh_step_label(self, row: int) -> None:
        if self._routine is None:
            return
        if 0 <= row < self._steps_list.count():
            self._steps_list.item(row).setText(
                f"{row + 1:>3}. {_describe_step(self._routine.steps[row])}"
            )

    # ---- 各 step 字段 ----

    def _build_travel_fields(
        self, form: QFormLayout, step: TravelStep, row: int
    ) -> None:
        # travel 下拉列出所有地图名 (含未录大地图位置的). 用户可以先写 routine
        # 引用一个还没录的地图, 等之后录入后再跑. 跑到时若目标 is_recorded=False,
        # runner._do_travel 会报错.
        combo = QComboBox()
        combo.setEditable(True)
        for name in self._known_map_locations:
            combo.addItem(name)
        combo.setCurrentText(step.to if not step.to.startswith("<") else "")

        def _changed(_=None):
            step.to = combo.currentText().strip()
            self._refresh_step_label(row)
            self._mark_dirty()

        combo.currentTextChanged.connect(_changed)
        form.addRow("to:", combo)

    def _build_move_fields(self, form: QFormLayout, step: MoveStep, row: int) -> None:
        path_widget = PathListWidget(
            step.path,
            ocr_callback=self._ocr_current_pos,
        )

        def _changed():
            step.path = path_widget.points()
            self._refresh_step_label(row)
            self._mark_dirty()

        path_widget.changed.connect(_changed)
        form.addRow("path:", path_widget)

    def _build_button_fields(
        self, form: QFormLayout, step: ButtonStep, row: int
    ) -> None:
        """
        ButtonStep 编辑器 (两段式布局, 镜像 ClickStep):

        【上半: name + skip + delay】
          - name 下拉: chat_1..N / table_1..N
          - skip / delay spinbox

        【下半: button 模板】 (与上半互斥)
          - "模板" 下拉: (不使用模板) + 已有模板列表
          - "+ 新建" / "管理" 两个独立按钮
          - 选中某个模板 → 上半整体禁用 + hint 提示

        交互:
          - step.template 不空: 模板下拉选中, 上半全禁用
          - step.template 空: 上半启用, 下半为 "(不使用模板)"
        """
        # ──── 上半 ────
        name_combo = QComboBox()
        name_combo.setEditable(True)
        for i in range(1, self._chat_btn_count + 1):
            name_combo.addItem(f"chat_{i}")
        for i in range(1, self._table_btn_count + 1):
            name_combo.addItem(f"table_{i}")
        name_combo.setCurrentText(step.name)

        skip_spin = QSpinBox()
        skip_spin.setRange(0, 20)
        skip_spin.setValue(step.skip)
        skip_spin.setToolTip("点完后再点几次 blank 跳过对话")

        delay_input = DelayInput(
            click_delays_provider=lambda: (
                self._movement_profile.click_delays
                if self._movement_profile is not None
                else None
            ),
            manage_callback=self._open_manage_click_delays_custom_dialog,
            parent=self,
        )
        delay_input.setToolTip(
            "整个动作完成后等待秒数; 可选自定义值或 ClickDelays 预设"
        )
        delay_input.set_value(step.delay, step.delay_preset)

        # ──── 下半 ────
        template_combo = QComboBox()
        self._populate_button_template_combo(template_combo, step)

        btn_new_template = QPushButton("+ 新建")
        btn_manage_template = QPushButton("管理")

        template_hint = QLabel("")
        template_hint.setStyleSheet("color: #666; font-size: 11px;")
        template_hint.setWordWrap(True)

        def _set_top_enabled(enabled: bool) -> None:
            name_combo.setEnabled(enabled)
            skip_spin.setEnabled(enabled)
            delay_input.setEnabled(enabled)

        def _refresh_template_state() -> None:
            """根据 step.template 决定上半禁用状态 + 模板 hint."""
            if step.template:
                tmpl = (
                    self._movement_profile.button_templates.get(step.template)
                    if self._movement_profile is not None
                    else None
                )
                _set_top_enabled(False)
                if tmpl is None:
                    template_hint.setText(
                        f"⚠ button 模板 {step.template!r} 未找到; 运行时会报错。"
                    )
                else:
                    template_hint.setText(
                        f"✓ 使用 button 模板 {step.template!r}: "
                        f"name={tmpl.name}, skip={tmpl.skip}, delay={tmpl.delay}s "
                        f"— 上方 name/skip/delay 在运行时被忽略"
                    )
            else:
                _set_top_enabled(True)
                template_hint.setText("")

        # ──── 上半信号 ────
        def _name_changed(_=None):
            step.name = name_combo.currentText().strip()
            self._refresh_step_label(row)
            self._mark_dirty()

        def _skip_changed(v: int):
            step.skip = v
            self._refresh_step_label(row)
            self._mark_dirty()

        def _delay_changed():
            seconds, preset = delay_input.value()
            step.delay = seconds
            step.delay_preset = preset
            self._refresh_step_label(row)
            self._mark_dirty()

        # ──── 下半信号 ────
        def _on_template_changed(_=None):
            data = template_combo.currentData()
            step.template = data
            if data is not None:
                # 选了模板 → 但 name 字段不能空 (互斥校验), 这里清空 name
                # (运行时不读 name; 但 dataclass __post_init__ 要求 name 或 template 至少一个非空)
                pass  # template 不空时, ButtonStep __post_init__ 允许 name 为空
            _refresh_template_state()
            self._refresh_step_label(row)
            self._mark_dirty()

        def _on_new_template_clicked():
            new_name = self._open_new_button_template_dialog(row)
            if new_name:
                step.template = new_name
                self._populate_button_template_combo(template_combo, step)
                _refresh_template_state()
                self._refresh_step_label(row)
                self._mark_dirty()

        def _on_manage_template_clicked():
            self._open_manage_button_templates_dialog()
            if step.template and not self._is_button_template_name_valid(step.template):
                step.template = None
            self._populate_button_template_combo(template_combo, step)
            _refresh_template_state()
            self._refresh_step_label(row)
            self._mark_dirty()

        name_combo.currentTextChanged.connect(_name_changed)
        skip_spin.valueChanged.connect(_skip_changed)
        delay_input.changed.connect(_delay_changed)
        template_combo.currentIndexChanged.connect(_on_template_changed)
        btn_new_template.clicked.connect(_on_new_template_clicked)
        btn_manage_template.clicked.connect(_on_manage_template_clicked)

        # ──── 布局 ────
        form.addRow("name:", name_combo)
        form.addRow("skip:", skip_spin)
        form.addRow("delay:", delay_input)

        form.addRow(_hline())
        form.addRow(QLabel("── button 模板 ──"))

        tpl_row = QHBoxLayout()
        tpl_row.addWidget(template_combo, 1)
        tpl_row.addWidget(btn_new_template)
        tpl_row.addWidget(btn_manage_template)
        form.addRow("模板:", _wrap(tpl_row))
        form.addRow("", template_hint)

        # 初始同步
        _refresh_template_state()

    def _populate_button_template_combo(
        self, combo: QComboBox, step: ButtonStep
    ) -> None:
        """button 模板下拉: (不使用模板) + 已有模板列表 (操作项是独立按钮)."""
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("(不使用模板)", None)
            if (
                self._movement_profile is not None
                and self._movement_profile.button_templates
            ):
                for tname in sorted(self._movement_profile.button_templates.keys()):
                    tmpl = self._movement_profile.button_templates[tname]
                    extras = []
                    if tmpl.skip:
                        extras.append(f"skip={tmpl.skip}")
                    if tmpl.delay:
                        extras.append(f"delay={tmpl.delay:g}")
                    extra_str = (" " + " ".join(extras)) if extras else ""
                    combo.addItem(f"★ {tname} — {tmpl.name}{extra_str}", tname)

            target_idx = 0
            if step.template:
                idx = combo.findData(step.template)
                if idx >= 0:
                    target_idx = idx
                else:
                    combo.addItem(f"★ {step.template} (未知模板)", step.template)
                    target_idx = combo.count() - 1
            combo.setCurrentIndex(target_idx)
        finally:
            combo.blockSignals(False)

    def _is_button_template_name_valid(self, name: str) -> bool:
        return (
            self._movement_profile is not None
            and name in self._movement_profile.button_templates
        )

    # ========================================================================
    # ClickStep: 编辑器分两段
    #
    # 上半 (位置预设 + 坐标 + skip + delay):
    #   下拉项分组:
    #     1. (自定义)                        ← step.preset=None
    #     2. 位置预设                        ← step.preset=name (内置 + ui.custom)
    #     3. ── 操作 ──                      ← "+ 新建位置预设" / "+ 管理位置预设"
    #
    # 下半 (click 模板, 与上半互斥):
    #   下拉项: (不使用模板) + 已有模板列表  ← step.template=name
    #   独立按钮: "+ 新建" / "管理"
    #
    # 用 itemData() 区分位置预设下拉里的 item 类型:
    #   None                                → 自定义模式
    #   字符串                              → 位置预设名
    #   {"kind": "action", "id": "..."}     → 触发对话框
    # 模板下拉的 itemData() 直接是 None 或模板名字符串。
    # ========================================================================
    _ACTION_NEW_PRESET = "new_preset"
    _ACTION_MANAGE_PRESET = "manage_preset"

    def _build_click_fields(self, form: QFormLayout, step: ClickStep, row: int) -> None:
        """
        ClickStep 编辑器 (两段式布局):

        【上半:位置预设区】
          - "位置预设" 下拉: (自定义) + 内置预设 + 自建预设 + (新建/管理) 动作
          - pos / skip / delay spinbox

        【下半:click 模板区】 (与位置预设互斥)
          - "模板" 下拉: (不使用模板) + 已有模板列表
          - "+ 新建" / "管理" 两个独立按钮 (不进下拉)
          - 选中某个模板 → 上半部分整体禁用 + 灰显, hint 提示模板内容
          - 选 "(不使用模板)" → 上半恢复启用, 走位置预设/自定义模式

        交互:
          - step.template 不空时: 模板下拉选中那个, 上半全禁用
          - step.preset 不空时: 上半下拉选中那个, pos 禁用 (skip/delay 仍可改)
          - 都为空时: 上半全启用, 下半为 "(不使用模板)"
        """
        # ──── 上半:位置预设 + 坐标 + skip/delay ────
        preset_combo = QComboBox()
        self._populate_position_preset_combo(preset_combo, step)

        preset_hint = QLabel("")
        preset_hint.setStyleSheet("color: #666; font-size: 11px;")
        preset_hint.setWordWrap(True)

        x_spin = QDoubleSpinBox()
        x_spin.setRange(0.0, 1.0)
        x_spin.setDecimals(4)
        x_spin.setSingleStep(0.001)
        x_spin.setValue(step.pos[0])
        y_spin = QDoubleSpinBox()
        y_spin.setRange(0.0, 1.0)
        y_spin.setDecimals(4)
        y_spin.setSingleStep(0.001)
        y_spin.setValue(step.pos[1])
        btn_pick = QPushButton("从游戏取")

        skip_spin = QSpinBox()
        skip_spin.setRange(0, 20)
        skip_spin.setValue(step.skip)
        skip_spin.setToolTip("点完后再点几次 blank_btn 跳过对话")

        delay_input = DelayInput(
            click_delays_provider=lambda: (
                self._movement_profile.click_delays
                if self._movement_profile is not None
                else None
            ),
            manage_callback=self._open_manage_click_delays_custom_dialog,
            parent=self,
        )
        delay_input.setToolTip("点击后等待秒数; 可选自定义值或 ClickDelays 预设")
        delay_input.set_value(step.delay, step.delay_preset)

        # ──── 下半:click 模板下拉 + 新建/管理按钮 ────
        template_combo = QComboBox()
        self._populate_template_combo(template_combo, step)

        btn_new_template = QPushButton("+ 新建")
        btn_manage_template = QPushButton("管理")

        template_hint = QLabel("")
        template_hint.setStyleSheet("color: #666; font-size: 11px;")
        template_hint.setWordWrap(True)

        # 把 "上半禁用" 这件事抽出来, 因为 _refresh_template_state 也会用
        def _set_top_enabled(enabled: bool) -> None:
            preset_combo.setEnabled(enabled)
            x_spin.setEnabled(enabled and not step.preset)
            y_spin.setEnabled(enabled and not step.preset)
            btn_pick.setEnabled(enabled and not step.preset)
            skip_spin.setEnabled(enabled)
            delay_input.setEnabled(enabled)

        def _resolve_position_preset(
            name: Optional[str],
        ) -> Optional[tuple[float, float]]:
            if not name or self._movement_profile is None:
                return None
            if name == "character_pos":
                return self._movement_profile.character_pos
            return self._movement_profile.ui.resolve_single_point(name)

        def _refresh_preset_state() -> None:
            """非模板模式下, 根据 step.preset 调整上半 spinbox 启用与显示."""
            preset_name = preset_combo.currentData()
            if isinstance(preset_name, dict):  # 兜住操作项 (理论上不停留在这里)
                return
            is_preset = preset_name is not None
            x_spin.setEnabled(not is_preset)
            y_spin.setEnabled(not is_preset)
            btn_pick.setEnabled(not is_preset)

            x_spin.blockSignals(True)
            y_spin.blockSignals(True)
            try:
                if is_preset:
                    resolved = _resolve_position_preset(preset_name)
                    is_custom = (
                        self._movement_profile is not None
                        and preset_name in self._movement_profile.ui.custom
                    )
                    source = "自建" if is_custom else "内置/运动配置"
                    if resolved is not None:
                        x_spin.setValue(resolved[0])
                        y_spin.setValue(resolved[1])
                        preset_hint.setText(
                            f"✓ 位置预设 → ({resolved[0]:.4f}, {resolved[1]:.4f}) "
                            f"— {source}, 可在「运动配置」里调整"
                        )
                    else:
                        preset_hint.setText(
                            f"⚠ 当前运动配置里 {preset_name!r} 未录入或非单点；"
                            f"运行时会报错。"
                        )
                else:
                    x_spin.setValue(step.pos[0])
                    y_spin.setValue(step.pos[1])
                    preset_hint.setText("")
            finally:
                x_spin.blockSignals(False)
                y_spin.blockSignals(False)

        def _refresh_template_state() -> None:
            """根据 step.template 决定上半是否整体禁用 + 模板 hint 文字."""
            if step.template:
                tmpl = (
                    self._movement_profile.click_templates.get(step.template)
                    if self._movement_profile is not None
                    else None
                )
                _set_top_enabled(False)
                if tmpl is None:
                    template_hint.setText(
                        f"⚠ click 模板 {step.template!r} 未找到; 运行时会报错。"
                    )
                else:
                    if tmpl.position_preset:
                        resolved = _resolve_position_preset(tmpl.position_preset)
                        pos_str = f"位置预设 {tmpl.position_preset!r}"
                        if resolved is not None:
                            pos_str += f" → ({resolved[0]:.4f}, {resolved[1]:.4f})"
                    elif tmpl.pos is not None:
                        pos_str = f"字面坐标 ({tmpl.pos[0]:.4f}, {tmpl.pos[1]:.4f})"
                    else:
                        pos_str = "(位置未配置)"
                    template_hint.setText(
                        f"✓ 使用 click 模板 {step.template!r}: {pos_str}, "
                        f"skip={tmpl.skip}, delay={tmpl.delay}s — "
                        f"上方位置预设/坐标/skip/delay 在运行时被忽略"
                    )
            else:
                _set_top_enabled(True)
                _refresh_preset_state()  # 恢复上半时按 preset 模式刷新
                template_hint.setText("")

        # ──── 信号:上半 ────
        def _on_preset_changed(_=None):
            data = preset_combo.currentData()
            if isinstance(data, dict) and data.get("kind") == "action":
                action_id = data["id"]
                if action_id == self._ACTION_NEW_PRESET:
                    new_name = self._open_new_click_preset_dialog(row)
                    if new_name:
                        step.preset = new_name
                        step.template = None
                        self._populate_position_preset_combo(preset_combo, step)
                        _refresh_preset_state()
                        self._refresh_step_label(row)
                        self._mark_dirty()
                    else:
                        self._populate_position_preset_combo(preset_combo, step)
                elif action_id == self._ACTION_MANAGE_PRESET:
                    self._open_manage_click_presets_dialog()
                    if step.preset and not self._is_preset_name_valid(step.preset):
                        step.preset = None
                    self._populate_position_preset_combo(preset_combo, step)
                    _refresh_preset_state()
                    self._refresh_step_label(row)
                    self._mark_dirty()
                return
            # 普通预设切换
            step.preset = data  # None 或字符串
            step.template = None  # 选了上半就退出模板模式
            _refresh_preset_state()
            self._refresh_step_label(row)
            self._mark_dirty()

        def _on_pos_changed(_=None):
            step.pos = (x_spin.value(), y_spin.value())
            self._refresh_step_label(row)
            self._mark_dirty()

        def _on_skip_changed(v: int):
            step.skip = v
            self._refresh_step_label(row)
            self._mark_dirty()

        def _on_delay_changed():
            seconds, preset = delay_input.value()
            step.delay = seconds
            step.delay_preset = preset
            self._refresh_step_label(row)
            self._mark_dirty()

        def _on_pick():
            records = self._pick_points(1, [f"第 {row + 1} 步 click 位置"])
            if records:
                x_spin.setValue(records[0].nx)
                y_spin.setValue(records[0].ny)

        # ──── 信号:下半模板 ────
        def _on_template_changed(_=None):
            data = template_combo.currentData()
            # data: None(不使用模板) 或 模板名字符串
            step.template = data
            if data is not None:
                step.preset = None  # 进入模板模式 → 清掉 preset
            _refresh_template_state()
            self._refresh_step_label(row)
            self._mark_dirty()

        def _on_new_template_clicked():
            new_name = self._open_new_click_template_dialog(row)
            if new_name:
                step.template = new_name
                step.preset = None
                self._populate_template_combo(template_combo, step)
                _refresh_template_state()
                self._refresh_step_label(row)
                self._mark_dirty()

        def _on_manage_template_clicked():
            self._open_manage_click_templates_dialog()
            # 管理后 step.template 可能被改名/删除, 校验一下
            if step.template and not self._is_template_name_valid(step.template):
                step.template = None
            self._populate_template_combo(template_combo, step)
            _refresh_template_state()
            self._refresh_step_label(row)
            self._mark_dirty()

        preset_combo.currentIndexChanged.connect(_on_preset_changed)
        x_spin.valueChanged.connect(_on_pos_changed)
        y_spin.valueChanged.connect(_on_pos_changed)
        skip_spin.valueChanged.connect(_on_skip_changed)
        delay_input.changed.connect(_on_delay_changed)
        btn_pick.clicked.connect(_on_pick)
        template_combo.currentIndexChanged.connect(_on_template_changed)
        btn_new_template.clicked.connect(_on_new_template_clicked)
        btn_manage_template.clicked.connect(_on_manage_template_clicked)

        # ──── 布局:上半 ────
        form.addRow("位置预设:", preset_combo)
        form.addRow("", preset_hint)

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("x"))
        pos_row.addWidget(x_spin)
        pos_row.addWidget(QLabel("y"))
        pos_row.addWidget(y_spin)
        pos_row.addWidget(btn_pick)
        pos_row.addStretch(1)
        form.addRow("pos:", _wrap(pos_row))
        form.addRow("skip:", skip_spin)
        form.addRow("delay:", delay_input)

        # ──── 分隔 + 下半:click 模板 ────
        form.addRow(_hline())
        form.addRow(QLabel("── click 模板 ──"))

        tpl_row = QHBoxLayout()
        tpl_row.addWidget(template_combo, 1)
        tpl_row.addWidget(btn_new_template)
        tpl_row.addWidget(btn_manage_template)
        form.addRow("模板:", _wrap(tpl_row))
        form.addRow("", template_hint)

        # 初始同步 (template 优先, 没 template 才看 preset)
        _refresh_template_state()

    def _populate_position_preset_combo(
        self, combo: QComboBox, step: ClickStep
    ) -> None:
        """位置预设下拉: (自定义) + 内置 + 自建 + 新建/管理 动作。"""
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("(自定义)", None)
            for name, label in CLICK_PRESETS:
                combo.addItem(f"{name} — {label}", name)
            custom_names: list[str] = []
            if self._movement_profile is not None and self._movement_profile.ui.custom:
                combo.insertSeparator(combo.count())
                for cname in sorted(self._movement_profile.ui.custom.keys()):
                    combo.addItem(f"{cname} (自建)", cname)
                    custom_names.append(cname)
            combo.insertSeparator(combo.count())
            combo.addItem(
                "+ 新建位置预设…", {"kind": "action", "id": self._ACTION_NEW_PRESET}
            )
            if custom_names:
                combo.addItem(
                    "+ 管理位置预设…",
                    {"kind": "action", "id": self._ACTION_MANAGE_PRESET},
                )

            # 选中匹配 step.preset (注意: 在 template 模式下 step.preset 也可能不空,
            # 但下拉显示什么不重要 - 上半已被禁用)
            target_idx = 0
            if step.preset:
                idx = combo.findData(step.preset)
                if idx >= 0:
                    target_idx = idx
                else:
                    # yaml 里有但当前 movement_profile 里查不到 → 临时项保住值
                    combo.addItem(f"{step.preset} (未知)", step.preset)
                    target_idx = combo.count() - 1
            combo.setCurrentIndex(target_idx)
        finally:
            combo.blockSignals(False)

    def _populate_template_combo(self, combo: QComboBox, step: ClickStep) -> None:
        """click 模板下拉: (不使用模板) + 已有模板列表 (无操作项, 操作项是独立按钮)."""
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("(不使用模板)", None)
            template_names: list[str] = []
            if (
                self._movement_profile is not None
                and self._movement_profile.click_templates
            ):
                for tname in sorted(self._movement_profile.click_templates.keys()):
                    tmpl = self._movement_profile.click_templates[tname]
                    if tmpl.position_preset:
                        desc = tmpl.position_preset
                    elif tmpl.pos is not None:
                        desc = f"({tmpl.pos[0]:.2f},{tmpl.pos[1]:.2f})"
                    else:
                        desc = "?"
                    extras = []
                    if tmpl.skip:
                        extras.append(f"skip={tmpl.skip}")
                    if tmpl.delay:
                        extras.append(f"delay={tmpl.delay:g}")
                    extra_str = (" " + " ".join(extras)) if extras else ""
                    combo.addItem(f"★ {tname} — {desc}{extra_str}", tname)
                    template_names.append(tname)

            target_idx = 0
            if step.template:
                idx = combo.findData(step.template)
                if idx >= 0:
                    target_idx = idx
                else:
                    combo.addItem(f"★ {step.template} (未知模板)", step.template)
                    target_idx = combo.count() - 1
            combo.setCurrentIndex(target_idx)
        finally:
            combo.blockSignals(False)

    def _is_preset_name_valid(self, name: str) -> bool:
        if name == "character_pos" or name in CLICK_PRESET_NAMES:
            return True
        return (
            self._movement_profile is not None
            and name in self._movement_profile.ui.custom
        )

    def _is_template_name_valid(self, name: str) -> bool:
        return (
            self._movement_profile is not None
            and name in self._movement_profile.click_templates
        )

    def _is_delay_preset_valid(self, name: str) -> bool:
        """delay_preset / loop_interval_preset / sleep preset 都用同一份校验.
        合法范围: ClickDelays 内置 16 个字段 + custom 字典里的 key."""
        if self._movement_profile is None:
            return False
        cd = self._movement_profile.click_delays
        return name in cd._SUB_FIELDS or name in cd.custom

    # ========================================================================
    # ClickStep: 自定义预设 - 新建 / 管理
    # ========================================================================

    def _open_new_click_preset_dialog(self, row: int) -> Optional[str]:
        """
        弹"新建 click 预设"对话框, 录入名+坐标, 写入 movement_profile.ui.custom
        并保存到磁盘。成功返回新预设名, 取消返回 None。

        前置: 当前必须有 _movement_profile (无配置时不放进 click 编辑器, 但兜一下)
        """
        if self._movement_profile is None:
            QMessageBox.warning(
                self,
                "无运动配置",
                "当前分辨率没有运动配置, 无法创建预设。请先在主界面「运动配置」里建立。",
            )
            return None

        existing_names = self._all_existing_preset_names()
        dlg = _NewClickPresetDialog(
            parent=self,
            existing_names=existing_names,
            mumu=self._mumu,
            pick_points_fn=self._pick_points,
            row_label=f"第 {row + 1} 步",
        )
        if dlg.exec() != QDialog.Accepted:
            return None

        name, x, y = dlg.result()
        # 写入 profile + 保存到磁盘
        try:
            self._movement_profile.ui.custom[name] = (x, y)
            self._movement_profile.save()
        except Exception as e:
            log.exception("保存新建 click 预设失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            # 回滚内存
            self._movement_profile.ui.custom.pop(name, None)
            return None
        return name

    def _open_manage_click_presets_dialog(self) -> bool:
        """
        弹"管理自定义预设"对话框, 支持删除单个 custom 预设。
        改动会立刻保存到磁盘。返回 True 表示有改动 (需要重建下拉/标记 dirty)。
        """
        if self._movement_profile is None or not self._movement_profile.ui.custom:
            QMessageBox.information(self, "无可管理项", "当前没有自建预设。")
            return False
        dlg = _ManageClickPresetsDialog(
            parent=self,
            custom=self._movement_profile.ui.custom,
        )
        dlg.exec()  # 用户在对话框里直接操作 dict, 这里只看是否真的改了
        if not dlg.changed:
            return False
        # 持久化
        try:
            self._movement_profile.save()
        except Exception as e:
            log.exception("保存 click 预设变更失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return False
        return True

    def _all_existing_preset_names(self) -> set[str]:
        """收集所有已用预设名 (内置 + 自建 + 'character_pos' + UIPositions 非单点字段),
        给新建对话框做重名校验用。"""
        names = set(CLICK_PRESET_NAMES)
        names.add("character_pos")
        # 也防止与非单点字段重名 (chat_btn / table_btn / buy_item_grid)
        names.update({"chat_btn", "table_btn", "buy_item_grid"})
        if self._movement_profile is not None:
            names.update(self._movement_profile.ui.custom.keys())
        return names

    # ========================================================================
    # ClickStep: click 模板 - 新建 / 管理
    # ========================================================================

    def _open_new_click_template_dialog(self, row: int) -> Optional[str]:
        """
        弹"新建 click 模板"对话框, 录入 名字 + 位置 + skip + delay,
        写入 movement_profile.click_templates 并保存到磁盘。
        成功返回新模板名, 取消返回 None。
        """
        if self._movement_profile is None:
            QMessageBox.warning(
                self,
                "无运动配置",
                "当前没有运动配置, 无法创建 click 模板。请先在主界面「运动配置」里建立。",
            )
            return None

        existing_names = set(self._movement_profile.click_templates.keys())
        # 收集可选位置预设 (用于"引用预设"模式的下拉)
        position_preset_options: list[str] = ["character_pos"] + list(
            CLICK_PRESET_NAMES
        )
        position_preset_options.extend(sorted(self._movement_profile.ui.custom.keys()))

        dlg = _NewClickTemplateDialog(
            parent=self,
            existing_names=existing_names,
            position_preset_options=position_preset_options,
            mumu=self._mumu,
            pick_points_fn=self._pick_points,
            row_label=f"第 {row + 1} 步",
            click_delays_provider=lambda: (
                self._movement_profile.click_delays
                if self._movement_profile is not None
                else None
            ),
            manage_delays_callback=self._open_manage_click_delays_custom_dialog,
        )
        if dlg.exec() != QDialog.Accepted:
            return None

        name, template = dlg.result()
        try:
            self._movement_profile.click_templates[name] = template
            self._movement_profile.save()
        except Exception as e:
            log.exception("保存新建 click 模板失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            self._movement_profile.click_templates.pop(name, None)
            return None
        return name

    def _open_manage_click_templates_dialog(self) -> bool:
        """
        弹"管理 click 模板"对话框, 支持新建/编辑(含改名)/删除。
        返回 True 表示有改动 (上层用来 mark dirty + 刷新 UI)。
        """
        if self._movement_profile is None:
            QMessageBox.warning(
                self,
                "无运动配置",
                "当前没有运动配置, 无法管理 click 模板。",
            )
            return False
        # 即使没有现有模板也能进入对话框 (用户可能想直接新建)
        # 收集位置预设选项
        position_preset_options: list[str] = ["character_pos"] + list(
            CLICK_PRESET_NAMES
        )
        position_preset_options.extend(sorted(self._movement_profile.ui.custom.keys()))

        dlg = _ManageClickTemplatesDialog(
            parent=self,
            templates=self._movement_profile.click_templates,
            position_preset_options=position_preset_options,
            mumu=self._mumu,
            pick_points_fn=self._pick_points,
            rename_callback=self._rename_click_template_in_routines,
            click_delays_provider=lambda: (
                self._movement_profile.click_delays
                if self._movement_profile is not None
                else None
            ),
            manage_delays_callback=self._open_manage_click_delays_custom_dialog,
        )
        dlg.exec()
        if not dlg.changed:
            return False
        try:
            self._movement_profile.save()
        except Exception as e:
            log.exception("保存 click 模板变更失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return False
        return True

    # ========================================================================
    # ClickDelays.custom 自建延时预设: 增删改 + 改名扫描
    # ========================================================================

    def _open_manage_click_delays_custom_dialog(self) -> bool:
        """
        弹「管理自建延时预设」对话框, 支持新建/编辑(含改名)/删除。
        返回 True 表示有改动。
        """
        if self._movement_profile is None:
            QMessageBox.warning(
                self,
                "无运动配置",
                "当前没有运动配置, 无法管理自建延时预设。",
            )
            return False
        builtin_names = set(self._movement_profile.click_delays._SUB_FIELDS) | {
            "default"
        }
        dlg = _ManageClickDelaysCustomDialog(
            parent=self,
            click_delays=self._movement_profile.click_delays,
            builtin_names=builtin_names,
            rename_callback=self._rename_click_delays_custom_in_routines,
        )
        dlg.exec()
        if not dlg.changed:
            return False
        try:
            self._movement_profile.save()
        except Exception as e:
            log.exception("保存自建延时预设变更失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return False
        return True

    def _rename_click_delays_custom_in_routines(
        self, old_name: str, new_name: str
    ) -> int:
        """
        全局扫描 routines/ yaml 把所有引用 old_name 的 delay_preset / loop_interval_preset
        / SleepStep.preset 替换为 new_name. 返回受影响文件数.

        受影响的 yaml 字段:
          - 顶层 loop_interval_preset
          - steps[*].delay_preset (click / button)
          - steps[*].preset (sleep)
        以及内存里的当前 routine.
        """
        affected = 0
        for yaml_path in self._routines_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("跳过无法解析的 yaml: %s (%s)", yaml_path, e)
                continue
            if not isinstance(data, dict):
                continue
            changed = False
            # 顶层 loop_interval_preset
            if data.get("loop_interval_preset") == old_name:
                data["loop_interval_preset"] = new_name
                changed = True
            # steps
            steps = data.get("steps")
            if isinstance(steps, list):
                for s in steps:
                    if not isinstance(s, dict):
                        continue
                    t = s.get("type")
                    # click / button: delay_preset
                    if t in ("click", "button") and s.get("delay_preset") == old_name:
                        s["delay_preset"] = new_name
                        changed = True
                    # sleep: preset
                    if t == "sleep" and s.get("preset") == old_name:
                        s["preset"] = new_name
                        changed = True
            if not changed:
                continue
            bak = yaml_path.with_suffix(".yaml.bak")
            shutil.copy(yaml_path, bak)
            yaml_path.write_text(
                yaml.safe_dump(
                    data,
                    allow_unicode=True,
                    sort_keys=False,
                    indent=2,
                    default_flow_style=None,
                ),
                encoding="utf-8",
            )
            affected += 1

        # 内存当前 routine
        if self._routine is not None:
            mem_changed = False
            if self._routine.loop_interval_preset == old_name:
                self._routine.loop_interval_preset = new_name
                mem_changed = True
            for s in self._routine.steps:
                if (
                    isinstance(s, (ClickStep, ButtonStep))
                    and s.delay_preset == old_name
                ):
                    s.delay_preset = new_name
                    mem_changed = True
                if isinstance(s, SleepStep) and s.preset == old_name:
                    s.preset = new_name
                    mem_changed = True
            if mem_changed:
                self._mark_dirty()
                self._refresh_steps_list()

        return affected

    def _rename_click_template_in_routines(self, old_name: str, new_name: str) -> int:
        """
        全局扫描 self._routines_dir 下所有 yaml, 把 ClickStep.template == old_name
        的引用改成 new_name. 返回受影响文件数. 异常向上抛.

        实现细节:
          - 用 yaml safe_load + safe_dump (不做文本替换, 避免误伤)
          - 操作前给受影响文件备份 .yaml.bak (保留最近一份, 之前的 .bak 会被覆盖)
          - 跳过当前打开的 routine (它的 step 已经在内存里, 直接改内存; 文件
            会在用户下次保存时被覆盖, 那时改动会丢. 这里仍然改文件, 但提示用户)

        风险: yaml round-trip 会改文件格式 (空格/引号/紧凑度). 这是已知代价.
        """
        affected = 0
        for yaml_path in self._routines_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("跳过无法解析的 yaml: %s (%s)", yaml_path, e)
                continue
            if not isinstance(data, dict):
                continue
            steps = data.get("steps")
            if not isinstance(steps, list):
                continue
            changed = False
            for s in steps:
                if (
                    isinstance(s, dict)
                    and s.get("type") == "click"
                    and s.get("template") == old_name
                ):
                    s["template"] = new_name
                    changed = True
            if not changed:
                continue
            # 备份并写入
            bak = yaml_path.with_suffix(".yaml.bak")
            shutil.copy(yaml_path, bak)
            yaml_path.write_text(
                yaml.safe_dump(
                    data,
                    allow_unicode=True,
                    sort_keys=False,
                    indent=2,
                    default_flow_style=None,
                ),
                encoding="utf-8",
            )
            affected += 1

        # 当前打开的 routine 在内存里也改一下 (如果引用了)
        if self._routine is not None:
            for s in self._routine.steps:
                if isinstance(s, ClickStep) and s.template == old_name:
                    s.template = new_name
                    self._mark_dirty()
                    self._refresh_steps_list()

        return affected

    # ========================================================================
    # ButtonStep: button 模板 - 新建 / 管理 / 改名
    # ========================================================================

    def _open_new_button_template_dialog(self, row: int) -> Optional[str]:
        """
        弹"新建 button 模板"对话框, 录入名字 + 引用的 chat_N/table_N + skip + delay,
        写入 movement_profile.button_templates 并保存。成功返回新模板名。
        """
        if self._movement_profile is None:
            QMessageBox.warning(
                self,
                "无运动配置",
                "当前没有运动配置, 无法创建 button 模板。",
            )
            return None
        existing_names = set(self._movement_profile.button_templates.keys())
        button_name_options = [
            f"chat_{i}" for i in range(1, self._chat_btn_count + 1)
        ] + [f"table_{i}" for i in range(1, self._table_btn_count + 1)]
        dlg = _NewButtonTemplateDialog(
            parent=self,
            existing_names=existing_names,
            button_name_options=button_name_options,
            click_delays_provider=lambda: (
                self._movement_profile.click_delays
                if self._movement_profile is not None
                else None
            ),
            manage_delays_callback=self._open_manage_click_delays_custom_dialog,
        )
        if dlg.exec() != QDialog.Accepted:
            return None
        name, template = dlg.result()
        try:
            self._movement_profile.button_templates[name] = template
            self._movement_profile.save()
        except Exception as e:
            log.exception("保存新建 button 模板失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            self._movement_profile.button_templates.pop(name, None)
            return None
        return name

    def _open_manage_button_templates_dialog(self) -> bool:
        """弹"管理 button 模板"对话框, 支持新建/编辑(含改名)/删除。"""
        if self._movement_profile is None:
            QMessageBox.warning(
                self,
                "无运动配置",
                "当前没有运动配置, 无法管理 button 模板。",
            )
            return False
        button_name_options = [
            f"chat_{i}" for i in range(1, self._chat_btn_count + 1)
        ] + [f"table_{i}" for i in range(1, self._table_btn_count + 1)]
        dlg = _ManageButtonTemplatesDialog(
            parent=self,
            templates=self._movement_profile.button_templates,
            button_name_options=button_name_options,
            rename_callback=self._rename_button_template_in_routines,
            click_delays_provider=lambda: (
                self._movement_profile.click_delays
                if self._movement_profile is not None
                else None
            ),
            manage_delays_callback=self._open_manage_click_delays_custom_dialog,
        )
        dlg.exec()
        if not dlg.changed:
            return False
        try:
            self._movement_profile.save()
        except Exception as e:
            log.exception("保存 button 模板变更失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return False
        return True

    def _rename_button_template_in_routines(self, old_name: str, new_name: str) -> int:
        """
        全局扫描 self._routines_dir 下所有 yaml, 把 ButtonStep.template == old_name
        的引用改成 new_name. 返回受影响文件数. 异常向上抛.

        实现: 镜像 _rename_click_template_in_routines, 只是匹配 type==button.
        """
        affected = 0
        for yaml_path in self._routines_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("跳过无法解析的 yaml: %s (%s)", yaml_path, e)
                continue
            if not isinstance(data, dict):
                continue
            steps = data.get("steps")
            if not isinstance(steps, list):
                continue
            changed = False
            for s in steps:
                if (
                    isinstance(s, dict)
                    and s.get("type") == "button"
                    and s.get("template") == old_name
                ):
                    s["template"] = new_name
                    changed = True
            if not changed:
                continue
            bak = yaml_path.with_suffix(".yaml.bak")
            shutil.copy(yaml_path, bak)
            yaml_path.write_text(
                yaml.safe_dump(
                    data,
                    allow_unicode=True,
                    sort_keys=False,
                    indent=2,
                    default_flow_style=None,
                ),
                encoding="utf-8",
            )
            affected += 1

        # 内存中当前 routine
        if self._routine is not None:
            for s in self._routine.steps:
                if isinstance(s, ButtonStep) and s.template == old_name:
                    s.template = new_name
                    self._mark_dirty()
                    self._refresh_steps_list()

        return affected

    def _build_buy_fields(self, form: QFormLayout, step: BuyStep, row: int) -> None:
        items_widget = BuyItemsWidget(step.items)

        def _changed():
            step.items = items_widget.items()
            self._refresh_step_label(row)
            self._mark_dirty()

        items_widget.changed.connect(_changed)
        form.addRow("items:", items_widget)

    def _build_sleep_fields(self, form: QFormLayout, step: SleepStep, row: int) -> None:
        """
        SleepStep 编辑器: 复用 DelayInput 提供"自定义秒数 / 预设引用"二选一。
        SleepStep 的 preset 字段语义就是 ClickDelays 字段名 (内置 + custom).
        """
        delay_input = DelayInput(
            click_delays_provider=lambda: (
                self._movement_profile.click_delays
                if self._movement_profile is not None
                else None
            ),
            manage_callback=self._open_manage_click_delays_custom_dialog,
            parent=self,
            max_seconds=600.0,
        )
        delay_input.set_value(step.seconds, step.preset)

        def _on_changed():
            seconds, preset = delay_input.value()
            step.seconds = seconds
            step.preset = preset
            self._refresh_step_label(row)
            self._mark_dirty()

        delay_input.changed.connect(_on_changed)
        form.addRow("seconds:", delay_input)

    def _build_include_fields(
        self, form: QFormLayout, step: IncludeStep, row: int
    ) -> None:
        """
        IncludeStep 编辑器:
          routine: 下拉 (列出 config/routines/*.yaml 的 stem)，editable
          自动跳过当前 routine 自己（防直接自引用）
          下方加一行 hint 简述子 routine 信息
        """
        cur_path = self._routine.path if self._routine else None
        cur_stem = cur_path.stem if cur_path else None

        combo = QComboBox()
        combo.setEditable(True)
        # 列出所有 routine yaml stem，跳过当前 routine 自身
        for p in list_routines(self._routines_dir):
            if cur_path is not None and p == cur_path:
                continue
            combo.addItem(p.stem)
        combo.setCurrentText(step.routine if not step.routine.startswith("<") else "")

        # 一个简短 hint label
        hint = QLabel("")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")

        def _refresh_hint():
            target = combo.currentText().strip()
            if not target or target.startswith("<"):
                hint.setText("")
                return
            if cur_stem and target == cur_stem:
                hint.setText("⚠ 引用自身，保存时会被拦截")
                return
            # 看看文件是否存在
            cand = self._routines_dir / f"{target}.yaml"
            if cand.exists():
                try:
                    sub = Routine.load(cand)
                    hint.setText(
                        f"✓ 找到 {cand.name}（{len(sub.steps)} 步）— "
                        f"loop_count/loop_interval/starting_map 字段会被忽略"
                    )
                except Exception as e:
                    hint.setText(f"⚠ 加载 {cand.name} 失败: {e}")
            else:
                hint.setText(f"⚠ 当前未找到 {cand.name}（runner 执行时若不存在会报错）")

        def _changed(_=None):
            step.routine = combo.currentText().strip()
            self._refresh_step_label(row)
            self._mark_dirty()
            _refresh_hint()

        combo.currentTextChanged.connect(_changed)
        form.addRow("routine:", combo)
        form.addRow("", hint)
        _refresh_hint()

    def _build_wait_common_fields(self, form: QFormLayout, step, row: int) -> None:
        thr = QDoubleSpinBox()
        thr.setRange(0, 1)
        thr.setDecimals(4)
        thr.setSingleStep(0.001)
        thr.setValue(step.threshold)

        def _thr_changed(v: float):
            step.threshold = v
            self._refresh_step_label(row)
            self._mark_dirty()

        thr.valueChanged.connect(_thr_changed)
        form.addRow("threshold:", thr)

        mw = QDoubleSpinBox()
        mw.setRange(0, 60)
        mw.setDecimals(2)
        mw.setSingleStep(0.5)
        mw.setValue(step.max_wait)

        def _mw_changed(v: float):
            step.max_wait = v
            self._refresh_step_label(row)
            self._mark_dirty()

        mw.valueChanged.connect(_mw_changed)
        form.addRow("max_wait:", mw)

        fps = QDoubleSpinBox()
        fps.setRange(1, 60)
        fps.setDecimals(1)
        fps.setSingleStep(1.0)
        fps.setValue(step.fps)

        def _fps_changed(v: float):
            step.fps = v
            self._refresh_step_label(row)
            self._mark_dirty()

        fps.valueChanged.connect(_fps_changed)
        form.addRow("fps:", fps)

    def _build_enter_map_fields(
        self, form: QFormLayout, step: EnterMapStep, row: int
    ) -> None:
        # enter_map 只是逻辑标记"现在在哪张地图", 不需要大地图传送数据;
        # 用 _known_map_locations 让只录了 map_size 的 mover-only 地图也能选.
        combo = QComboBox()
        combo.setEditable(True)
        for name in self._known_map_locations:
            combo.addItem(name)
        combo.setCurrentText(step.map if not step.map.startswith("<") else "")

        def _changed(_=None):
            step.map = combo.currentText().strip()
            self._refresh_step_label(row)
            self._mark_dirty()

        combo.currentTextChanged.connect(_changed)
        form.addRow("map:", combo)

    # ========================================================================
    # 取位置（PositionPicker 复用）
    # ========================================================================

    def _pick_points(self, n: int, labels: list[str]):
        dlg = PositionPickerDialog(
            self._mumu,
            parent=self,
            selection_mode=True,
            expected_count=n,
            selection_labels=labels,
        )
        if dlg.exec() == QDialog.Accepted:
            return dlg.result_records()
        return None

    # ========================================================================
    # 关闭
    # ========================================================================

    def closeEvent(self, ev) -> None:
        if not self._maybe_save_dirty():
            ev.ignore()
            return
        super().closeEvent(ev)


# =============================================================================
# Helpers
# =============================================================================


def _wrap(lay) -> QWidget:
    w = QWidget()
    w.setLayout(lay)
    return w


def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Sunken)
    return f


# =============================================================================
# DelayInput: 复合控件 (spinbox + 预设下拉 + 「管理」按钮)
# =============================================================================


class DelayInput(QWidget):
    """
    "字面值" / "预设" 二选一的延时输入控件。

    布局: [spinbox] [▼ 预设下拉] [管理]
    下拉项:
      - "(自定义)"      → 用 spinbox 值, value()=(seconds, None)
      - 其他预设名      → 用预设, value()=(0.0, preset_name)
      - "+ 管理…"       → 触发 manage_callback, 之后下拉重建

    交互:
      - 选了预设 → spinbox 禁用 + 显示解析后的秒数
      - 选了自定义 → spinbox 启用 + 还原 user 上次输入

    信号:
      - changed: 任意值/模式改变时发出 (供上层 mark_dirty / refresh_label)
    """

    changed = Signal()

    _ACTION_MANAGE = "<MANAGE>"

    def __init__(
        self,
        click_delays_provider,  # callable: () -> ClickDelays
        manage_callback,  # callable: () -> bool (有改动时返回 True)
        *,
        parent: Optional[QWidget] = None,
        max_seconds: float = 60.0,
    ) -> None:
        super().__init__(parent)
        self._click_delays_provider = click_delays_provider
        self._manage_callback = manage_callback

        self._spin = QDoubleSpinBox()
        self._spin.setRange(0.0, max_seconds)
        self._spin.setDecimals(2)
        self._spin.setSingleStep(0.1)

        self._combo = QComboBox()

        self._btn_manage = QPushButton("管理")
        self._btn_manage.setMaximumWidth(54)
        self._btn_manage.setToolTip("管理 ClickDelays.custom 自建延时预设")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._spin)
        layout.addWidget(self._combo, 1)
        layout.addWidget(self._btn_manage)

        self._populate_combo(None)

        self._spin.valueChanged.connect(self._on_spin_changed)
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        self._btn_manage.clicked.connect(self._on_manage_clicked)

    def _populate_combo(self, select_preset: Optional[str]) -> None:
        """重建下拉项. select_preset 为 None 表示自定义."""
        self._combo.blockSignals(True)
        try:
            self._combo.clear()
            self._combo.addItem("(自定义)", None)
            click_delays = self._click_delays_provider()
            if click_delays is not None:
                names = click_delays.all_preset_names()
                if names:
                    self._combo.insertSeparator(self._combo.count())
                    for name in names:
                        seconds = click_delays.resolve(name)
                        is_custom = name in click_delays.custom
                        marker = "★ " if is_custom else ""
                        self._combo.addItem(f"{marker}{name} ({seconds:.2f}s)", name)
            self._combo.insertSeparator(self._combo.count())
            self._combo.addItem("+ 管理 custom 预设…", self._ACTION_MANAGE)

            # 选中匹配项
            target_idx = 0
            if select_preset:
                idx = self._combo.findData(select_preset)
                if idx >= 0:
                    target_idx = idx
                else:
                    self._combo.addItem(f"{select_preset} (未知)", select_preset)
                    target_idx = self._combo.count() - 1
            self._combo.setCurrentIndex(target_idx)
        finally:
            self._combo.blockSignals(False)

    def set_value(self, seconds: float, preset: Optional[str]) -> None:
        """外部用: 设置值 (preset 不空 → 预设模式; 否则 spinbox 模式)."""
        # 阻止信号避免外部 set 触发 changed
        self.blockSignals(True)
        try:
            self._spin.blockSignals(True)
            self._spin.setValue(float(seconds or 0.0))
            self._spin.blockSignals(False)
            self._populate_combo(preset)
            self._refresh_spin_state()
        finally:
            self.blockSignals(False)

    def value(self) -> tuple[float, Optional[str]]:
        """返回 (seconds, preset_name). preset 不空时 seconds 不应被使用."""
        data = self._combo.currentData()
        if data is None:
            return float(self._spin.value()), None
        if data == self._ACTION_MANAGE:
            # 不应进入此分支 (action 项不持久停留), 兜底返回自定义
            return float(self._spin.value()), None
        return 0.0, data  # 预设模式

    def _refresh_spin_state(self) -> None:
        data = self._combo.currentData()
        is_preset = isinstance(data, str) and data != self._ACTION_MANAGE
        self._spin.setEnabled(not is_preset)
        if is_preset:
            click_delays = self._click_delays_provider()
            if click_delays is not None:
                seconds = click_delays.resolve(data)
                self._spin.blockSignals(True)
                try:
                    self._spin.setValue(seconds)
                finally:
                    self._spin.blockSignals(False)

    def _on_spin_changed(self, _v: float) -> None:
        # spinbox 改变只在自定义模式下意味着 "用户改值"
        if self._combo.currentData() is None:
            self.changed.emit()

    def _on_combo_changed(self, _idx: int) -> None:
        data = self._combo.currentData()
        if data == self._ACTION_MANAGE:
            # 触发管理对话框, 然后重建下拉 (custom 可能已经变了)
            current_select = None  # 之前的选中, 但管理后可能不存在了
            # 取管理前的选中 (除了 action 本身) - 用 spin/preset 状态来判
            # 简化: 管理后回到 (自定义), 让用户重新选
            try:
                self._manage_callback()
            finally:
                self._populate_combo(current_select)
                self._refresh_spin_state()
                self.changed.emit()
            return
        # 普通切换
        self._refresh_spin_state()
        self.changed.emit()

    def _on_manage_clicked(self) -> None:
        """点「管理」按钮: 同 combo 里的 +管理 项."""
        try:
            self._manage_callback()
        finally:
            # 保留当前选中 (如果还在)
            data = self._combo.currentData()
            preset = (
                data if isinstance(data, str) and data != self._ACTION_MANAGE else None
            )
            self._populate_combo(preset)
            self._refresh_spin_state()
            self.changed.emit()

    def refresh_options(self) -> None:
        """外部用: 当 ClickDelays.custom 被改 (例如别处管理对话框关闭后), 刷新下拉项."""
        data = self._combo.currentData()
        preset = data if isinstance(data, str) and data != self._ACTION_MANAGE else None
        self._populate_combo(preset)
        self._refresh_spin_state()


def _set_spin_quietly(
    x_spin: QDoubleSpinBox,
    y_spin: QDoubleSpinBox,
    skip_spin: QSpinBox,
    delay_spin: QDoubleSpinBox,
    tmpl,  # ClickTemplate or None
    *,
    resolved: Optional[tuple[float, float]] = None,
    step_skip: Optional[int] = None,
    step_delay: Optional[float] = None,
) -> None:
    """
    用 blockSignals 安全地把显示值塞进 spinbox 们 (避免触发 valueChanged → 污染 step)。

    两种调用模式:
      - 模板模式: tmpl 给 ClickTemplate, 从中取 skip/delay; pos 部分若是字面坐标
                  直接显示, 若是引用预设则不动 spinbox (用户看 hint 即可)
      - 非模板:  tmpl=None, kwargs 显式给 resolved (pos) + step_skip + step_delay
                 resolved=None 表示位置无法解析, 此时保留 spin 现值不动
    """
    for sp in (x_spin, y_spin, skip_spin, delay_spin):
        sp.blockSignals(True)
    try:
        if tmpl is not None:
            # 模板模式: skip/delay 必填
            skip_spin.setValue(int(tmpl.skip))
            delay_spin.setValue(float(tmpl.delay))
            # 位置部分: 字面坐标直接显示; 引用预设则保留 spin 现值
            # (调用方已通过 hint 标签把信息展示给用户了)
            if tmpl.pos is not None:
                x_spin.setValue(float(tmpl.pos[0]))
                y_spin.setValue(float(tmpl.pos[1]))
        else:
            if resolved is not None:
                x_spin.setValue(float(resolved[0]))
                y_spin.setValue(float(resolved[1]))
            if step_skip is not None:
                skip_spin.setValue(int(step_skip))
            if step_delay is not None:
                delay_spin.setValue(float(step_delay))
    finally:
        for sp in (x_spin, y_spin, skip_spin, delay_spin):
            sp.blockSignals(False)


# =============================================================================
# 对话框: 新建 click 自定义预设
# =============================================================================


# 预设名校验:
#   - 允许中英文 + 数字 + 下划线
#   - 首字符不能是数字 (避免与"全数字"混淆 + 一些 yaml 工具的解析坑)
#   - 长度 1~32 (UI 显示和 yaml 序列化都不会被撑爆)
# 排除连字符/点/空格/冒号等 yaml 保留字符 - 否则 yaml 序列化得加引号
_PRESET_NAME_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z_][\u4e00-\u9fa5A-Za-z0-9_]{0,31}$")


class _NewClickPresetDialog(QDialog):
    """
    弹小窗:输入预设名 + x/y + 「从游戏取」+ 确定/取消。
    成功 accept 后通过 result() 拿到 (name, x, y)。

    校验:
      - 名字非空 + 符合 _PRESET_NAME_RE
      - 不与 existing_names (内置 + character_pos + 非单点 + 已有 custom) 冲突
      - x / y 在 [0, 1]
    所有校验失败时实时禁用「确定」按钮 + 显示提示。
    """

    def __init__(
        self,
        *,
        parent: QWidget,
        existing_names: set[str],
        mumu: Mumu,
        pick_points_fn,  # callable(n, labels) -> list[Record]
        row_label: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建 click 预设")
        self.resize(420, 200)
        self._existing = existing_names
        self._mumu = mumu
        self._pick = pick_points_fn
        self._row_label = row_label
        self._build_ui()
        self._validate()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        root.addLayout(form)

        # 名字
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("中英文/数字/下划线均可，如 张三丰_pos")
        self._name_edit.textChanged.connect(self._validate)
        form.addRow("名字:", self._name_edit)

        # x / y / 取
        self._x_spin = QDoubleSpinBox()
        self._x_spin.setRange(0.0, 1.0)
        self._x_spin.setDecimals(4)
        self._x_spin.setSingleStep(0.001)
        self._x_spin.setValue(0.5)

        self._y_spin = QDoubleSpinBox()
        self._y_spin.setRange(0.0, 1.0)
        self._y_spin.setDecimals(4)
        self._y_spin.setSingleStep(0.001)
        self._y_spin.setValue(0.5)

        btn_pick = QPushButton("从游戏取")
        btn_pick.clicked.connect(self._on_pick)

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("x"))
        pos_row.addWidget(self._x_spin)
        pos_row.addWidget(QLabel("y"))
        pos_row.addWidget(self._y_spin)
        pos_row.addWidget(btn_pick)
        pos_row.addStretch(1)
        form.addRow("坐标:", _wrap(pos_row))

        # hint
        self._hint = QLabel("")
        self._hint.setStyleSheet("color: #c0392b; font-size: 11px;")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        # 按钮
        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    def _on_pick(self) -> None:
        records = self._pick(1, [f"{self._row_label} click 位置 (新预设)"])
        if records:
            self._x_spin.setValue(records[0].nx)
            self._y_spin.setValue(records[0].ny)

    def _validate(self) -> None:
        name = self._name_edit.text().strip()
        msg = ""
        ok = True
        if not name:
            ok = False
            msg = ""  # 空名字不报错, 只是不能确定
        elif not _PRESET_NAME_RE.match(name):
            ok = False
            msg = "名字只能含中英文、数字、下划线; " "不能以数字开头, 长度 1~32"
        elif name in self._existing:
            ok = False
            msg = f"{name!r} 已存在 (内置或自建预设)"
        self._hint.setText(msg)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def result(self) -> tuple[str, float, float]:  # type: ignore[override]
        return (
            self._name_edit.text().strip(),
            self._x_spin.value(),
            self._y_spin.value(),
        )


# =============================================================================
# 对话框: 管理已有 click 自定义预设
# =============================================================================


class _ManageClickPresetsDialog(QDialog):
    """
    管理 movement_profile.ui.custom 字典:
      - 列出所有 custom 预设, 显示坐标
      - 选中某项可点「删除选中」
    用户操作直接改传入的 dict 引用; 上层自行决定何时 save。
    """

    def __init__(
        self, *, parent: QWidget, custom: dict[str, tuple[float, float]]
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理自定义 click 预设")
        self.resize(440, 360)
        self._custom = custom
        self.changed = False
        self._build_ui()
        self._refresh_list()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(
            QLabel("当前所有自建 click 预设。删除后该预设在所有 routine 里失效。")
        )

        self._list = QListWidget()
        root.addWidget(self._list, 1)

        btns = QHBoxLayout()
        self._btn_delete = QPushButton("删除选中")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_delete.setEnabled(False)
        btns.addWidget(self._btn_delete)
        btns.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_close)
        root.addLayout(btns)

        self._list.itemSelectionChanged.connect(
            lambda: self._btn_delete.setEnabled(self._list.currentItem() is not None)
        )

    def _refresh_list(self) -> None:
        self._list.clear()
        for name in sorted(self._custom.keys()):
            x, y = self._custom[name]
            item = QListWidgetItem(f"{name}    ({x:.4f}, {y:.4f})")
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)

    def _on_delete(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        ans = QMessageBox.question(
            self,
            "确认删除",
            f"确认删除自建预设 {name!r} ?\n所有引用该预设的 routine 步骤将在运行时报错, "
            f"需要手动改回自定义或换一个预设。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._custom.pop(name, None)
        self.changed = True
        self._refresh_list()


# =============================================================================
# 对话框: 新建 click 模板
# =============================================================================


class _NewClickTemplateDialog(QDialog):
    """
    新建 / 编辑 click 模板。
    成功 accept 后通过 result() 拿到 (name, ClickTemplate)。
    通过 was_renamed() 判断改名后旧名字是什么 (编辑模式专用)。

    构造参数:
      - existing_template: None=新建, 给定 ClickTemplate=编辑模式 (预填字段, 允许改名)
      - existing_names:    重名校验 (编辑模式时调用方应排除当前名字)
    位置二选一通过 radio 互斥, 切换时禁用对应控件。
    """

    def __init__(
        self,
        *,
        parent: QWidget,
        existing_names: set[str],
        position_preset_options: list[str],
        mumu: Mumu,
        pick_points_fn,
        row_label: str,
        click_delays_provider,
        manage_delays_callback,
        existing_name: Optional[str] = None,
        existing_template: Optional[ClickTemplate] = None,
    ) -> None:
        super().__init__(parent)
        self._is_edit = existing_template is not None
        self.setWindowTitle("编辑 click 模板" if self._is_edit else "新建 click 模板")
        self.resize(440, 360)
        self._existing = existing_names
        self._mumu = mumu
        self._pick = pick_points_fn
        self._row_label = row_label
        self._position_preset_options = position_preset_options
        self._click_delays_provider = click_delays_provider
        self._manage_delays_callback = manage_delays_callback
        self._original_name = existing_name  # 编辑模式时记录原名以便外层判断改名
        self._build_ui()
        # 预填 (编辑模式)
        if existing_template is not None:
            assert existing_name is not None
            self._name_edit.setText(existing_name)
            if existing_template.position_preset:
                self._radio_preset.setChecked(True)
                idx = self._preset_combo.findData(existing_template.position_preset)
                if idx < 0:
                    # 预设名当前不在选项里, 临时项保住值
                    self._preset_combo.addItem(
                        f"{existing_template.position_preset} (未知)",
                        existing_template.position_preset,
                    )
                    idx = self._preset_combo.count() - 1
                self._preset_combo.setCurrentIndex(idx)
            elif existing_template.pos is not None:
                self._radio_pos.setChecked(True)
                self._x_spin.setValue(existing_template.pos[0])
                self._y_spin.setValue(existing_template.pos[1])
            self._skip_spin.setValue(existing_template.skip)
            self._delay_input.set_value(
                existing_template.delay, existing_template.delay_preset
            )
        self._on_mode_changed()
        self._validate()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        root.addLayout(form)

        # 名字
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("中英文/数字/下划线均可，如 跳3次对话")
        self._name_edit.textChanged.connect(self._validate)
        form.addRow("名字:", self._name_edit)

        # 位置: radio 1 (引用位置预设) + 下拉
        self._radio_preset = QRadioButton("引用位置预设")
        self._preset_combo = QComboBox()
        for n in self._position_preset_options:
            self._preset_combo.addItem(n, n)

        preset_row = QHBoxLayout()
        preset_row.addWidget(self._radio_preset)
        preset_row.addWidget(self._preset_combo, 1)
        form.addRow("位置 ①:", _wrap(preset_row))

        # 位置: radio 2 (字面坐标) + x/y/取
        self._radio_pos = QRadioButton("字面坐标")
        self._x_spin = QDoubleSpinBox()
        self._x_spin.setRange(0.0, 1.0)
        self._x_spin.setDecimals(4)
        self._x_spin.setSingleStep(0.001)
        self._x_spin.setValue(0.5)
        self._y_spin = QDoubleSpinBox()
        self._y_spin.setRange(0.0, 1.0)
        self._y_spin.setDecimals(4)
        self._y_spin.setSingleStep(0.001)
        self._y_spin.setValue(0.5)
        self._btn_pick = QPushButton("从游戏取")
        self._btn_pick.clicked.connect(self._on_pick)

        pos_row = QHBoxLayout()
        pos_row.addWidget(self._radio_pos)
        pos_row.addWidget(QLabel("x"))
        pos_row.addWidget(self._x_spin)
        pos_row.addWidget(QLabel("y"))
        pos_row.addWidget(self._y_spin)
        pos_row.addWidget(self._btn_pick)
        form.addRow("位置 ②:", _wrap(pos_row))

        # 互斥
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._radio_preset, 0)
        self._mode_group.addButton(self._radio_pos, 1)
        self._radio_preset.setChecked(True)  # 默认引用预设
        self._mode_group.buttonClicked.connect(lambda _: self._on_mode_changed())

        # skip / delay
        self._skip_spin = QSpinBox()
        self._skip_spin.setRange(0, 20)
        form.addRow("skip:", self._skip_spin)
        self._delay_input = DelayInput(
            click_delays_provider=self._click_delays_provider,
            manage_callback=self._manage_delays_callback,
            parent=self,
        )
        form.addRow("delay:", self._delay_input)

        # hint + 按钮
        self._hint = QLabel("")
        self._hint.setStyleSheet("color: #c0392b; font-size: 11px;")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    def _on_mode_changed(self) -> None:
        is_preset = self._radio_preset.isChecked()
        self._preset_combo.setEnabled(is_preset)
        self._x_spin.setEnabled(not is_preset)
        self._y_spin.setEnabled(not is_preset)
        self._btn_pick.setEnabled(not is_preset)

    def _on_pick(self) -> None:
        records = self._pick(1, [f"{self._row_label} click 模板位置"])
        if records:
            self._x_spin.setValue(records[0].nx)
            self._y_spin.setValue(records[0].ny)

    def _validate(self) -> None:
        name = self._name_edit.text().strip()
        msg, ok = "", True
        if not name:
            ok, msg = False, ""
        elif not _PRESET_NAME_RE.match(name):
            ok = False
            msg = "名字只能含中英文、数字、下划线; 不能以数字开头, 长度 1~32"
        elif name in self._existing:
            ok = False
            msg = f"click 模板 {name!r} 已存在"
        self._hint.setText(msg)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def result(self) -> tuple[str, "ClickTemplate"]:  # type: ignore[override]
        name = self._name_edit.text().strip()
        seconds, preset = self._delay_input.value()
        if self._radio_preset.isChecked():
            tmpl = ClickTemplate(
                position_preset=self._preset_combo.currentData(),
                skip=int(self._skip_spin.value()),
                delay=seconds,
                delay_preset=preset,
            )
        else:
            tmpl = ClickTemplate(
                pos=(self._x_spin.value(), self._y_spin.value()),
                skip=int(self._skip_spin.value()),
                delay=seconds,
                delay_preset=preset,
            )
        return name, tmpl

    @property
    def original_name(self) -> Optional[str]:
        """编辑模式下的原名字; 新建模式返回 None."""
        return self._original_name


# =============================================================================
# 对话框: 管理已有 click 模板
# =============================================================================


class _ManageClickTemplatesDialog(QDialog):
    """
    管理 movement_profile.click_templates:
      - 列出所有模板 (位置 + skip + delay 一行展示)
      - 「+ 新建」: 弹 _NewClickTemplateDialog 新建模式
      - 「编辑选中」(也支持双击列表项): 弹 _NewClickTemplateDialog 编辑模式
        - 改名时调 rename_callback 让上层做"全局扫描 routines/ 同步引用"
      - 「删除选中」: 删除模板 (引用断了, 运行时报错, 由用户自行处理)
      - 「关闭」: dlg accept

    所有改动直接在传入的 templates dict 上做; 由上层在退出后自行 save()。
    用户改名时调用方应该负责扫描 routines/ 并替换引用。
    """

    def __init__(
        self,
        *,
        parent: QWidget,
        templates: dict,  # dict[str, ClickTemplate]
        position_preset_options: list[str],
        mumu: Mumu,
        pick_points_fn,
        rename_callback,  # (old_name, new_name) -> int (受影响 routine 数, 出错抛异常)
        click_delays_provider,
        manage_delays_callback,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理 click 模板")
        self.resize(520, 400)
        self._templates = templates
        self._position_preset_options = position_preset_options
        self._mumu = mumu
        self._pick = pick_points_fn
        self._rename_callback = rename_callback
        self._click_delays_provider = click_delays_provider
        self._manage_delays_callback = manage_delays_callback
        self.changed = False
        self._build_ui()
        self._refresh_list()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(
            QLabel(
                "管理所有 click 模板。双击列表项可编辑; 改名会同步扫描 "
                "routines/ 替换引用; 删除后引用该模板的步骤运行时会报错。"
            )
        )

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _it: self._on_edit())
        self._list.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self._list, 1)

        btns = QHBoxLayout()
        self._btn_new = QPushButton("+ 新建")
        self._btn_new.clicked.connect(self._on_new)
        btns.addWidget(self._btn_new)

        self._btn_edit = QPushButton("编辑选中")
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_edit.setEnabled(False)
        btns.addWidget(self._btn_edit)

        self._btn_delete = QPushButton("删除选中")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_delete.setEnabled(False)
        btns.addWidget(self._btn_delete)

        btns.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_close)
        root.addLayout(btns)

    def _update_buttons(self) -> None:
        has_sel = self._list.currentItem() is not None
        self._btn_edit.setEnabled(has_sel)
        self._btn_delete.setEnabled(has_sel)

    def _refresh_list(self, select_name: Optional[str] = None) -> None:
        self._list.clear()
        for name in sorted(self._templates.keys()):
            t = self._templates[name]
            if t.position_preset:
                pos_str = f"位置={t.position_preset}"
            elif t.pos is not None:
                pos_str = f"pos=({t.pos[0]:.4f},{t.pos[1]:.4f})"
            else:
                pos_str = "位置=?"
            extras = []
            if t.skip:
                extras.append(f"skip={t.skip}")
            if t.delay:
                extras.append(f"delay={t.delay:g}s")
            extra_str = (" " + " ".join(extras)) if extras else ""
            item = QListWidgetItem(f"{name}    {pos_str}{extra_str}")
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)
            if select_name == name:
                self._list.setCurrentItem(item)
        self._update_buttons()

    def _on_new(self) -> None:
        existing_names = set(self._templates.keys())
        dlg = _NewClickTemplateDialog(
            parent=self,
            existing_names=existing_names,
            position_preset_options=self._position_preset_options,
            mumu=self._mumu,
            pick_points_fn=self._pick,
            row_label="(模板)",
            click_delays_provider=self._click_delays_provider,
            manage_delays_callback=self._manage_delays_callback,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        name, template = dlg.result()
        self._templates[name] = template
        self.changed = True
        self._refresh_list(select_name=name)

    def _on_edit(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        old_name: str = item.data(Qt.UserRole)
        old_template = self._templates.get(old_name)
        if old_template is None:
            return  # 防御
        # 编辑时排除自己当前名字 (允许保留同名)
        existing_names = set(self._templates.keys()) - {old_name}
        dlg = _NewClickTemplateDialog(
            parent=self,
            existing_names=existing_names,
            position_preset_options=self._position_preset_options,
            mumu=self._mumu,
            pick_points_fn=self._pick,
            row_label="(编辑模板)",
            click_delays_provider=self._click_delays_provider,
            manage_delays_callback=self._manage_delays_callback,
            existing_name=old_name,
            existing_template=old_template,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        new_name, new_template = dlg.result()

        # 改名: 调 rename_callback 扫描 routines/ 同步替换
        if new_name != old_name:
            try:
                affected_count = self._rename_callback(old_name, new_name)
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "改名失败",
                    f"扫描 routines/ 替换引用时出错:\n{type(e).__name__}: {e}\n"
                    f"模板未改名。",
                )
                return
            # 提示影响范围
            if affected_count > 0:
                QMessageBox.information(
                    self,
                    "改名完成",
                    f"已同步修改 {affected_count} 个 routine 文件中的引用。\n"
                    f"如有其它 routine 编辑器窗口正在打开, 请关闭并重新打开以加载最新内容。",
                )
            # dict 替换
            del self._templates[old_name]
        # 字段更新 (无论改名与否)
        self._templates[new_name] = new_template
        self.changed = True
        self._refresh_list(select_name=new_name)

    def _on_delete(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        ans = QMessageBox.question(
            self,
            "确认删除",
            f"确认删除 click 模板 {name!r} ?\n"
            f"所有引用该模板的 routine 步骤将在运行时报错。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._templates.pop(name, None)
        self.changed = True
        self._refresh_list()


# =============================================================================
# 对话框: 新建 button 模板
# =============================================================================


class _NewButtonTemplateDialog(QDialog):
    """
    新建 / 编辑 button 模板。
    成功 accept 后通过 result() 拿到 (name, ButtonTemplate)。
    通过 original_name 判断改名 (编辑模式).

    构造参数:
      - existing_template: None=新建, 给定 ButtonTemplate=编辑模式
      - existing_names:    重名校验 (编辑模式时调用方应排除当前名字)
      - button_name_options: chat_1/...table_1/... 下拉选项
    """

    def __init__(
        self,
        *,
        parent: QWidget,
        existing_names: set[str],
        button_name_options: list[str],
        click_delays_provider,
        manage_delays_callback,
        existing_name: Optional[str] = None,
        existing_template: Optional[ButtonTemplate] = None,
    ) -> None:
        super().__init__(parent)
        self._is_edit = existing_template is not None
        self.setWindowTitle("编辑 button 模板" if self._is_edit else "新建 button 模板")
        self.resize(380, 240)
        self._existing = existing_names
        self._button_name_options = button_name_options
        self._click_delays_provider = click_delays_provider
        self._manage_delays_callback = manage_delays_callback
        self._original_name = existing_name
        self._build_ui()
        if existing_template is not None:
            assert existing_name is not None
            self._name_edit.setText(existing_name)
            idx = self._btn_name_combo.findData(existing_template.name)
            if idx < 0:
                # 选项里没有 (movement profile chat/table count 改过) → 临时项
                self._btn_name_combo.addItem(
                    f"{existing_template.name} (未知)", existing_template.name
                )
                idx = self._btn_name_combo.count() - 1
            self._btn_name_combo.setCurrentIndex(idx)
            self._skip_spin.setValue(existing_template.skip)
            self._delay_input.set_value(
                existing_template.delay, existing_template.delay_preset
            )
        self._validate()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        root.addLayout(form)

        # 名字
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("中英文/数字/下划线均可，如 进入小屋")
        self._name_edit.textChanged.connect(self._validate)
        form.addRow("名字:", self._name_edit)

        # button name 下拉
        self._btn_name_combo = QComboBox()
        for n in self._button_name_options:
            self._btn_name_combo.addItem(n, n)
        form.addRow("button name:", self._btn_name_combo)

        # skip / delay
        self._skip_spin = QSpinBox()
        self._skip_spin.setRange(0, 20)
        form.addRow("skip:", self._skip_spin)
        self._delay_input = DelayInput(
            click_delays_provider=self._click_delays_provider,
            manage_callback=self._manage_delays_callback,
            parent=self,
        )
        form.addRow("delay:", self._delay_input)

        # hint + 按钮
        self._hint = QLabel("")
        self._hint.setStyleSheet("color: #c0392b; font-size: 11px;")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    def _validate(self) -> None:
        name = self._name_edit.text().strip()
        msg, ok = "", True
        if not name:
            ok, msg = False, ""
        elif not _PRESET_NAME_RE.match(name):
            ok = False
            msg = "名字只能含中英文、数字、下划线; 不能以数字开头, 长度 1~32"
        elif name in self._existing:
            ok = False
            msg = f"button 模板 {name!r} 已存在"
        self._hint.setText(msg)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def result(self) -> tuple[str, "ButtonTemplate"]:  # type: ignore[override]
        seconds, preset = self._delay_input.value()
        return (
            self._name_edit.text().strip(),
            ButtonTemplate(
                name=self._btn_name_combo.currentData() or "",
                skip=int(self._skip_spin.value()),
                delay=seconds,
                delay_preset=preset,
            ),
        )

    @property
    def original_name(self) -> Optional[str]:
        return self._original_name


# =============================================================================
# 对话框: 管理 button 模板
# =============================================================================


class _ManageButtonTemplatesDialog(QDialog):
    """
    管理 movement_profile.button_templates: 新建 / 编辑 (含改名) / 删除。
    与 _ManageClickTemplatesDialog 同模式。
    """

    def __init__(
        self,
        *,
        parent: QWidget,
        templates: dict,  # dict[str, ButtonTemplate]
        button_name_options: list[str],
        rename_callback,
        click_delays_provider,
        manage_delays_callback,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理 button 模板")
        self.resize(480, 360)
        self._templates = templates
        self._button_name_options = button_name_options
        self._rename_callback = rename_callback
        self._click_delays_provider = click_delays_provider
        self._manage_delays_callback = manage_delays_callback
        self.changed = False
        self._build_ui()
        self._refresh_list()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(
            QLabel(
                "管理所有 button 模板。双击可编辑; 改名会同步扫描 routines/ 替换引用; "
                "删除后引用该模板的步骤运行时会报错。"
            )
        )

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _it: self._on_edit())
        self._list.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self._list, 1)

        btns = QHBoxLayout()
        self._btn_new = QPushButton("+ 新建")
        self._btn_new.clicked.connect(self._on_new)
        btns.addWidget(self._btn_new)

        self._btn_edit = QPushButton("编辑选中")
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_edit.setEnabled(False)
        btns.addWidget(self._btn_edit)

        self._btn_delete = QPushButton("删除选中")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_delete.setEnabled(False)
        btns.addWidget(self._btn_delete)

        btns.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_close)
        root.addLayout(btns)

    def _update_buttons(self) -> None:
        has_sel = self._list.currentItem() is not None
        self._btn_edit.setEnabled(has_sel)
        self._btn_delete.setEnabled(has_sel)

    def _refresh_list(self, select_name: Optional[str] = None) -> None:
        self._list.clear()
        for name in sorted(self._templates.keys()):
            t = self._templates[name]
            extras = []
            if t.skip:
                extras.append(f"skip={t.skip}")
            if t.delay:
                extras.append(f"delay={t.delay:g}s")
            extra_str = (" " + " ".join(extras)) if extras else ""
            item = QListWidgetItem(f"{name}    name={t.name}{extra_str}")
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)
            if select_name == name:
                self._list.setCurrentItem(item)
        self._update_buttons()

    def _on_new(self) -> None:
        existing_names = set(self._templates.keys())
        dlg = _NewButtonTemplateDialog(
            parent=self,
            existing_names=existing_names,
            button_name_options=self._button_name_options,
            click_delays_provider=self._click_delays_provider,
            manage_delays_callback=self._manage_delays_callback,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        name, template = dlg.result()
        self._templates[name] = template
        self.changed = True
        self._refresh_list(select_name=name)

    def _on_edit(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        old_name = item.data(Qt.UserRole)
        old_template = self._templates.get(old_name)
        if old_template is None:
            return
        existing_names = set(self._templates.keys()) - {old_name}
        dlg = _NewButtonTemplateDialog(
            parent=self,
            existing_names=existing_names,
            button_name_options=self._button_name_options,
            click_delays_provider=self._click_delays_provider,
            manage_delays_callback=self._manage_delays_callback,
            existing_name=old_name,
            existing_template=old_template,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        new_name, new_template = dlg.result()

        if new_name != old_name:
            try:
                affected_count = self._rename_callback(old_name, new_name)
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "改名失败",
                    f"扫描 routines/ 替换引用时出错:\n{type(e).__name__}: {e}\n"
                    f"模板未改名。",
                )
                return
            if affected_count > 0:
                QMessageBox.information(
                    self,
                    "改名完成",
                    f"已同步修改 {affected_count} 个 routine 文件中的引用。\n"
                    f"如有其它 routine 编辑器窗口正在打开, 请关闭并重新打开。",
                )
            del self._templates[old_name]
        self._templates[new_name] = new_template
        self.changed = True
        self._refresh_list(select_name=new_name)

    def _on_delete(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        ans = QMessageBox.question(
            self,
            "确认删除",
            f"确认删除 button 模板 {name!r} ?\n"
            f"所有引用该模板的 routine 步骤将在运行时报错。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._templates.pop(name, None)
        self.changed = True
        self._refresh_list()


# =============================================================================
# 对话框: 管理 ClickDelays.custom 自建延时预设
# =============================================================================


class _NewClickDelayCustomDialog(QDialog):
    """新建 / 编辑单个自建延时预设 (名字 + 秒数)。"""

    def __init__(
        self,
        *,
        parent: QWidget,
        existing_names: set[str],
        builtin_names: set[str],
        existing_name: Optional[str] = None,
        existing_seconds: float = 0.5,
    ) -> None:
        super().__init__(parent)
        self._is_edit = existing_name is not None
        self.setWindowTitle("编辑自建延时预设" if self._is_edit else "新建自建延时预设")
        self.resize(360, 160)
        self._existing = existing_names
        self._builtin = builtin_names
        self._original_name = existing_name
        self._build_ui()
        if existing_name is not None:
            self._name_edit.setText(existing_name)
            self._spin.setValue(existing_seconds)
        else:
            self._spin.setValue(existing_seconds)
        self._validate()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        root.addLayout(form)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("中英文/数字/下划线均可，如 切场动画")
        self._name_edit.textChanged.connect(self._validate)
        form.addRow("名字:", self._name_edit)

        self._spin = QDoubleSpinBox()
        self._spin.setRange(0.0, 60.0)
        self._spin.setDecimals(2)
        self._spin.setSingleStep(0.1)
        form.addRow("秒数:", self._spin)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color: #c0392b; font-size: 11px;")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    def _validate(self) -> None:
        name = self._name_edit.text().strip()
        msg, ok = "", True
        if not name:
            ok, msg = False, ""
        elif not _PRESET_NAME_RE.match(name):
            ok, msg = False, "名字只能含中英文、数字、下划线; 不能以数字开头, 长度 1~32"
        elif name in self._builtin:
            ok, msg = False, f"{name!r} 是内置延时分类名, 不能与之重名"
        elif name in self._existing:
            ok, msg = False, f"自建延时预设 {name!r} 已存在"
        self._hint.setText(msg)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def result(self) -> tuple[str, float]:  # type: ignore[override]
        return self._name_edit.text().strip(), float(self._spin.value())

    @property
    def original_name(self) -> Optional[str]:
        return self._original_name


class _ManageClickDelaysCustomDialog(QDialog):
    """
    管理 ClickDelays.custom 自建延时预设: 新建 / 编辑 (含改名) / 删除。
    内置 16 个字段不在此对话框管理 (用户应去「运动配置」改值)。
    改名时调 rename_callback 扫描 routines/ 同步替换引用 (delay_preset /
    loop_interval_preset / sleep step 的 preset)。
    """

    def __init__(
        self,
        *,
        parent: QWidget,
        click_delays,  # ClickDelays
        builtin_names: set[str],
        rename_callback,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理自建延时预设")
        self.resize(480, 360)
        self._click_delays = click_delays
        self._custom = click_delays.custom  # 直接持有 dict 引用
        self._builtin_names = builtin_names
        self._rename_callback = rename_callback
        self.changed = False
        self._build_ui()
        self._refresh_list()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(
            QLabel(
                "管理 ClickDelays.custom 自建延时预设。双击可编辑; "
                "改名会同步扫描 routines/ 替换引用; "
                "删除后引用该预设的步骤运行时回退到 default。"
            )
        )
        root.addWidget(
            QLabel(
                "内置 16 个延时字段 (button / blank_skip / fly / ...) 不在此管理, "
                "请去「运动配置」改值。"
            )
        )

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _it: self._on_edit())
        self._list.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self._list, 1)

        btns = QHBoxLayout()
        self._btn_new = QPushButton("+ 新建")
        self._btn_new.clicked.connect(self._on_new)
        btns.addWidget(self._btn_new)

        self._btn_edit = QPushButton("编辑选中")
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_edit.setEnabled(False)
        btns.addWidget(self._btn_edit)

        self._btn_delete = QPushButton("删除选中")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_delete.setEnabled(False)
        btns.addWidget(self._btn_delete)

        btns.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_close)
        root.addLayout(btns)

    def _update_buttons(self) -> None:
        has_sel = self._list.currentItem() is not None
        self._btn_edit.setEnabled(has_sel)
        self._btn_delete.setEnabled(has_sel)

    def _refresh_list(self, select_name: Optional[str] = None) -> None:
        self._list.clear()
        for name in sorted(self._custom.keys()):
            item = QListWidgetItem(f"{name}    {self._custom[name]:.2f}s")
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)
            if select_name == name:
                self._list.setCurrentItem(item)
        self._update_buttons()

    def _on_new(self) -> None:
        existing_names = set(self._custom.keys())
        dlg = _NewClickDelayCustomDialog(
            parent=self,
            existing_names=existing_names,
            builtin_names=self._builtin_names,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        name, seconds = dlg.result()
        self._custom[name] = seconds
        self.changed = True
        self._refresh_list(select_name=name)

    def _on_edit(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        old_name = item.data(Qt.UserRole)
        old_seconds = self._custom.get(old_name)
        if old_seconds is None:
            return
        existing_names = set(self._custom.keys()) - {old_name}
        dlg = _NewClickDelayCustomDialog(
            parent=self,
            existing_names=existing_names,
            builtin_names=self._builtin_names,
            existing_name=old_name,
            existing_seconds=old_seconds,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        new_name, new_seconds = dlg.result()

        if new_name != old_name:
            try:
                affected = self._rename_callback(old_name, new_name)
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "改名失败",
                    f"扫描 routines/ 替换引用时出错:\n{type(e).__name__}: {e}\n"
                    f"预设未改名。",
                )
                return
            if affected > 0:
                QMessageBox.information(
                    self,
                    "改名完成",
                    f"已同步修改 {affected} 个 routine 文件中的引用。\n"
                    f"如有其它 routine 编辑器窗口正在打开, 请关闭并重新打开。",
                )
            del self._custom[old_name]
        self._custom[new_name] = new_seconds
        self.changed = True
        self._refresh_list(select_name=new_name)

    def _on_delete(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        ans = QMessageBox.question(
            self,
            "确认删除",
            f"确认删除自建延时预设 {name!r} ?\n"
            f"所有引用该预设的步骤运行时会回退到 default 延时。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        self._custom.pop(name, None)
        self.changed = True
        self._refresh_list()
