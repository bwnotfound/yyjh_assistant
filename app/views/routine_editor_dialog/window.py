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
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
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
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from utils import Mumu

from app.core.ocr import CoordReader, TemplateOCR
from app.core.profiles import (
    DEFAULT_MOVEMENT_YAML_PATH,
    MovementProfile,
    MovementRegistry,
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
        skip = f" skip={s.skip}" if s.skip else ""
        delay = f" delay={s.delay}" if s.delay else ""
        return f"button {s.name}{skip}{delay}{at}"
    if t == "click":
        if s.preset:
            return f"click [{s.preset}]{at}"
        return f"click ({s.pos[0]:.3f}, {s.pos[1]:.3f}){at}"
    if t == "buy":
        head = ", ".join(f"({i},{q})" for i, q in s.items[:3])
        more = "..." if len(s.items) > 3 else ""
        return f"buy [{head}{more}] ({len(s.items)} 项){at}"
    if t == "sleep":
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
        self._movement_profile: Optional[MovementProfile] = None
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
            mov_reg = MovementRegistry.load(DEFAULT_MOVEMENT_YAML_PATH)
            mp = mov_reg.profiles.get(f"{self._mumu.device_w}x{self._mumu.device_h}")
            if mp is not None:
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
        self._loop_interval = QDoubleSpinBox()
        self._loop_interval.setRange(0, 600)
        self._loop_interval.setDecimals(1)
        self._loop_interval.valueChanged.connect(self._on_meta_changed)
        loop_row.addWidget(self._loop_interval)
        loop_row.addStretch(1)
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
        cur_stem = self._routine.path.stem if self._routine.path is not None else None
        for i, s in enumerate(self._routine.steps, start=1):
            if isinstance(s, TravelStep):
                if not s.to or s.to.startswith("<"):
                    return f"第 {i} 步 (travel): to 未填"
            elif isinstance(s, EnterMapStep):
                if not s.map or s.map.startswith("<"):
                    return f"第 {i} 步 (enter_map): map 未填"
            elif isinstance(s, ButtonStep):
                if not s.name or s.name.startswith("<"):
                    return f"第 {i} 步 (button): name 未填"
                if "_" not in s.name:
                    return (
                        f"第 {i} 步 (button): name={s.name!r} "
                        f"应为 chat_N / table_N 形式"
                    )
            elif isinstance(s, MoveStep) and not s.path:
                return f"第 {i} 步 (move): path 不能为空"
            elif isinstance(s, ClickStep):
                if s.preset and s.preset not in CLICK_PRESET_NAMES:
                    return (
                        f"第 {i} 步 (click): preset {s.preset!r} 非法；"
                        f"合法值: {sorted(CLICK_PRESET_NAMES)}"
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
            self._loop_interval.setValue(r.loop_interval if r else 0.0)
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
        self._routine.loop_interval = self._loop_interval.value()
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
        # name 下拉：chat_1..N + table_1..N
        combo = QComboBox()
        combo.setEditable(True)
        for i in range(1, self._chat_btn_count + 1):
            combo.addItem(f"chat_{i}")
        for i in range(1, self._table_btn_count + 1):
            combo.addItem(f"table_{i}")
        combo.setCurrentText(step.name)

        def _name_changed(_=None):
            step.name = combo.currentText().strip()
            self._refresh_step_label(row)
            self._mark_dirty()

        combo.currentTextChanged.connect(_name_changed)
        form.addRow("name:", combo)

        skip_spin = QSpinBox()
        skip_spin.setRange(0, 20)
        skip_spin.setValue(step.skip)
        skip_spin.setToolTip("点完后再点几次 blank 跳过对话")

        def _skip_changed(v: int):
            step.skip = v
            self._refresh_step_label(row)
            self._mark_dirty()

        skip_spin.valueChanged.connect(_skip_changed)
        form.addRow("skip:", skip_spin)

        delay_spin = QDoubleSpinBox()
        delay_spin.setRange(0, 60)
        delay_spin.setDecimals(2)
        delay_spin.setSingleStep(0.1)
        delay_spin.setValue(step.delay)
        delay_spin.setToolTip("整个动作完成后额外等待秒数")

        def _delay_changed(v: float):
            step.delay = v
            self._refresh_step_label(row)
            self._mark_dirty()

        delay_spin.valueChanged.connect(_delay_changed)
        form.addRow("delay:", delay_spin)

    def _build_click_fields(self, form: QFormLayout, step: ClickStep, row: int) -> None:
        """
        ClickStep 编辑器:
          - "预设" 下拉: (自定义) + 各 ui 按钮预设
          - 选预设: x/y spinbox 禁用编辑,但显示解析后的坐标
          - 选自定义: x/y spinbox 启用 + 「从游戏取」可用
          - skip / delay 与模式无关
        """
        # ─ 预设下拉 ─
        preset_combo = QComboBox()
        preset_combo.addItem("(自定义)", None)
        for name, label in CLICK_PRESETS:
            preset_combo.addItem(f"{name} — {label}", name)

        initial_idx = 0
        if step.preset:
            found = preset_combo.findData(step.preset)
            if found >= 0:
                initial_idx = found
            else:
                # yaml 里 preset 写错或将来扩展过 → 临时项保住值不丢
                preset_combo.addItem(f"{step.preset} (未知)", step.preset)
                initial_idx = preset_combo.count() - 1
        preset_combo.setCurrentIndex(initial_idx)

        # ─ 预设解析 hint ─
        preset_hint = QLabel("")
        preset_hint.setStyleSheet("color: #666; font-size: 11px;")
        preset_hint.setWordWrap(True)

        # ─ x / y / 取位置 ─
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

        def _resolve_preset_pos(name: Optional[str]) -> Optional[tuple[float, float]]:
            """编辑器侧用:把 preset 名解析为 (x, y)。无法解析返回 None"""
            if not name or self._movement_profile is None:
                return None
            if name == "character_pos":
                return self._movement_profile.character_pos
            v = getattr(self._movement_profile.ui, name, None)
            if isinstance(v, tuple) and len(v) == 2:
                return v
            return None

        def _refresh_preset_state():
            """根据当前 preset 模式刷新 spinbox 启用状态 + hint"""
            preset_name = preset_combo.currentData()
            is_preset = preset_name is not None
            x_spin.setEnabled(not is_preset)
            y_spin.setEnabled(not is_preset)
            btn_pick.setEnabled(not is_preset)

            # 切换显示值时不要触发 _pos_changed (那会污染 step.pos)
            x_spin.blockSignals(True)
            y_spin.blockSignals(True)
            try:
                if is_preset:
                    resolved = _resolve_preset_pos(preset_name)
                    if resolved is not None:
                        x_spin.setValue(resolved[0])
                        y_spin.setValue(resolved[1])
                        preset_hint.setText(
                            f"✓ 解析为 ({resolved[0]:.4f}, {resolved[1]:.4f}) "
                            f"— 可在「运动配置」里调整"
                        )
                    else:
                        preset_hint.setText(
                            f"⚠ 当前运动配置里 {preset_name!r} 未录入或非单点；"
                            f"运行时会报错。请去「运动配置」补录。"
                        )
                else:
                    # 切回自定义: spinbox 还原为 step.pos
                    x_spin.setValue(step.pos[0])
                    y_spin.setValue(step.pos[1])
                    preset_hint.setText("")
            finally:
                x_spin.blockSignals(False)
                y_spin.blockSignals(False)

        def _on_preset_changed(_=None):
            step.preset = preset_combo.currentData()
            _refresh_preset_state()
            self._refresh_step_label(row)
            self._mark_dirty()

        preset_combo.currentIndexChanged.connect(_on_preset_changed)
        form.addRow("预设:", preset_combo)
        form.addRow("", preset_hint)

        def _pos_changed(_=None):
            # 只在自定义模式下写回 step.pos —— preset 模式 spinbox 已 blockSignals 不会触发
            step.pos = (x_spin.value(), y_spin.value())
            self._refresh_step_label(row)
            self._mark_dirty()

        x_spin.valueChanged.connect(_pos_changed)
        y_spin.valueChanged.connect(_pos_changed)

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("x"))
        pos_row.addWidget(x_spin)
        pos_row.addWidget(QLabel("y"))
        pos_row.addWidget(y_spin)

        def _on_pick():
            records = self._pick_points(1, [f"第 {row + 1} 步 click 位置"])
            if records:
                x_spin.setValue(records[0].nx)
                y_spin.setValue(records[0].ny)

        btn_pick.clicked.connect(_on_pick)
        pos_row.addWidget(btn_pick)
        pos_row.addStretch(1)
        form.addRow("pos:", _wrap(pos_row))

        # 初始状态同步 (含 preset 模式下解析显示 + 禁用 spinbox)
        _refresh_preset_state()

        # ─ skip / delay ─
        skip_spin = QSpinBox()
        skip_spin.setRange(0, 20)
        skip_spin.setValue(step.skip)
        skip_spin.setToolTip("点完后再点几次 blank_btn 跳过对话")

        def _skip_changed(v: int):
            step.skip = v
            self._refresh_step_label(row)
            self._mark_dirty()

        skip_spin.valueChanged.connect(_skip_changed)
        form.addRow("skip:", skip_spin)

        delay_spin = QDoubleSpinBox()
        delay_spin.setRange(0, 60)
        delay_spin.setDecimals(2)
        delay_spin.setSingleStep(0.1)
        delay_spin.setValue(step.delay)

        def _delay_changed(v: float):
            step.delay = v
            self._refresh_step_label(row)
            self._mark_dirty()

        delay_spin.valueChanged.connect(_delay_changed)
        form.addRow("delay:", delay_spin)

    def _build_buy_fields(self, form: QFormLayout, step: BuyStep, row: int) -> None:
        items_widget = BuyItemsWidget(step.items)

        def _changed():
            step.items = items_widget.items()
            self._refresh_step_label(row)
            self._mark_dirty()

        items_widget.changed.connect(_changed)
        form.addRow("items:", items_widget)

    def _build_sleep_fields(self, form: QFormLayout, step: SleepStep, row: int) -> None:
        spin = QDoubleSpinBox()
        spin.setRange(0, 600)
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.setValue(step.seconds)

        def _changed(v: float):
            step.seconds = v
            self._refresh_step_label(row)
            self._mark_dirty()

        spin.valueChanged.connect(_changed)
        form.addRow("seconds:", spin)

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
