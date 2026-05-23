"""
反解 map_size 工具

前提:
  · character_pos 准确 (movement_profile)
  · block_size 误差小 (movement_profile.vision_sizes[当前视野])
  · view_area 准确 (movement_profile.map_view_area)

工作流:
  1. 由 map_registry_dialog 在编辑某地图时点"反解…"按钮唤起;
     地图名 + 当前 vision_size 由调用方锁定传入 (单地图反解, 不允许混合)
  2. 在游戏里把角色走到能"贴边"的位置 —— 越靠近地图屏幕边越好:
       走到屏幕**左**端 (W 激活) → 解 mh
       走到屏幕**右**端 (E 激活) → 解 mw
       走到屏幕**下**端 (S 激活) → 解 mw+mh (独立 sum 字段)
       走到屏幕**上**端 (N 激活) → 无信息
  3. 点 [取一次观测]:
       a. 工具截一帧 + OCR 取角色当前格坐标 (gx, gy)
       b. 弹放大窗, 点击"角色脚下中心"得屏幕实际位置 (px, py)
       c. solve_map_size_observation 反解出对应维度
  4. 重复 3 直到所需维度都解出 (或任一维度因路径不通无法观测).
  5. 点 [应用到主对话框]:
       三个字段独立: 本次反解出哪个就填回主对话框对应 spinbox, 没反解的不动
       (保持主对话框 spinbox 原值, 不再"填 999 占位").

跨多次观测同一维度的策略: 取最新值 (与 view_area_solver 一致).
S 观测的 sum 现在是独立的 map_size_sum 字段 (LocationRecord.map_size_sum),
runtime 里在完整 map_size 不可得时作为 fallback (只能修正 N/S 方向).

返回值: dialog.exec() 返回 QDialog.Accepted 时, dialog.accepted_outcome 为
MapSizeSolverOutcome(mw, mh, sum_); 取消则为 None. 字段 None 表示本次未反解
出该值, 调用方据此决定是否覆盖主对话框对应 spinbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from utils import Mumu

from app.core.ocr import CoordReader, TemplateOCR
from app.core.profiles import (
    MapSizeSolveResult,
    MovementConfig,
    VisionSpec,
    solve_map_size_observation,
)
from app.views.position_picker.window import ZoomedSnapshotPicker

log = logging.getLogger(__name__)


MINIMAP_TEMPLATE_DIR = Path("config/templates/minimap_coord")


# =============================================================================
# UI 层观测记录 (反解逻辑本身在 app.core.profiles)
# =============================================================================


@dataclass
class _Observation:
    """一次观测的完整记录 (用于历史展示)"""

    pre_pos: tuple[int, int]
    screen_pos: tuple[float, float]
    result: MapSizeSolveResult


@dataclass
class _PendingPick:
    """主窗截图 + OCR 完成后的待反解上下文 (子窗每次点击都用这份反解)"""

    pre_pos: tuple[int, int]


# =============================================================================
# 主对话框
# =============================================================================


# =============================================================================
# 应用结果 (返回给 MapRegistryDialog)
# =============================================================================


@dataclass
class MapSizeSolverOutcome:
    """
    反解工具的最终输出. 三个字段独立可选, 调用方按字段是否为 None 决定是否
    覆盖对应 LocationRecord 字段:
      · mw   ≠ None → 反解出独立 w, 应用到 map_size 的 w 维
      · mh   ≠ None → 反解出独立 h, 应用到 map_size 的 h 维
      · sum_ ≠ None → 反解出 w+h, 应用到 map_size_sum 字段

    一个观测最多同时提供 (mw + sum_) 或 (mh + sum_) 两个值 (X 轴 + Y 轴各一);
    要同时拿到 mw 和 mh 需要 ≥2 次观测 (左右各贴一次).
    """

    mw: Optional[int] = None
    mh: Optional[int] = None
    sum_: Optional[int] = None

    @property
    def has_any(self) -> bool:
        return self.mw is not None or self.mh is not None or self.sum_ is not None


class MapSizeSolverDialog(QDialog):
    """
    反解 map_size 的交互工具.

    构造参数 (由 map_registry_dialog 传入, 锁定上下文):
      mumu             : Mumu 实例
      movement_profile : 当前分辨率的运动配置 (含 character_pos / view_area / vision_sizes)
      vision_name      : 当前地图选定的视野档位名 (从 LocationRecord.vision_size 读)
      map_label        : 地图名 (仅显示用)
      initial_size     : 主对话框 spinbox 当前值 (mw, mh) 或 None;
                         作为初始候选 (本次未反解出来时回填, 但不会自动作为 outcome).
      initial_sum      : 主对话框 sum spinbox 当前值 或 None; 同上.

    使用方式:
      dlg = MapSizeSolverDialog(mumu, mp, "小", "黑水沟", initial_size=(0, 0))
      if dlg.exec() == QDialog.Accepted:
          outcome = dlg.accepted_outcome  # MapSizeSolverOutcome
          # outcome.mw / mh / sum_ 任一非 None 就独立写到对应字段
    """

    def __init__(
        self,
        mumu: Mumu,
        movement_profile: MovementConfig,
        vision_name: str,
        map_label: str,
        initial_size: Optional[tuple[int, int]] = None,
        initial_sum: Optional[int] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"反解 map_size — {map_label}")
        self.resize(620, 700)

        self._mumu = mumu
        self._movement_profile = movement_profile
        self._vision_name = vision_name
        self._map_label = map_label

        # 校验依赖参数 (调用方负责拦, 这里再保险一次)
        self._vision_spec: Optional[VisionSpec] = movement_profile.vision_sizes.get(
            vision_name
        )
        self._view_area = movement_profile.map_view_area

        # 候选累积 (取最新策略: 列表里追加, 显示用最后一项)
        self._mw_latest: Optional[float] = None
        self._mh_latest: Optional[float] = None
        self._mw_plus_mh_latest: Optional[float] = None
        self._observations: list[_Observation] = []

        # 初始 spinbox 值: 若 != 0 则视为已有值, 显示作为参考但不进 observations
        # (避免被用户当作"上次反解的结果")
        self._initial_mw: Optional[int] = None
        self._initial_mh: Optional[int] = None
        self._initial_sum: Optional[int] = None
        if initial_size is not None:
            mw0, mh0 = initial_size
            if mw0 > 0:
                self._initial_mw = int(mw0)
            if mh0 > 0:
                self._initial_mh = int(mh0)
        if initial_sum is not None and initial_sum > 0:
            self._initial_sum = int(initial_sum)

        # 应用结果 (exec accept 后由调用方读)
        self.accepted_outcome: Optional[MapSizeSolverOutcome] = None

        # OCR 链路 lazy
        self._template_ocr: Optional[TemplateOCR] = None
        self._coord_reader: Optional[CoordReader] = None

        # 放大子窗 + 待反解上下文
        self._zoom_picker: Optional[ZoomedSnapshotPicker] = None
        self._pending: Optional[_PendingPick] = None

        self._build_ui()
        self._refresh_state_display()

    # -------------------------------------------------------------------------
    # OCR 链路
    # -------------------------------------------------------------------------

    def _get_coord_reader(self) -> CoordReader:
        if self._coord_reader is not None:
            return self._coord_reader
        roi = self._movement_profile.minimap_coord_roi
        if roi is None:
            raise RuntimeError(
                "运动配置里未录入 minimap_coord_roi (小地图坐标 ROI). "
                "请先用主界面「ROI 截取工具」录入."
            )
        if self._template_ocr is None:
            try:
                self._template_ocr = TemplateOCR.from_dir(MINIMAP_TEMPLATE_DIR)
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"OCR 字符模板目录无可用模板: {MINIMAP_TEMPLATE_DIR}\n原因: {e}"
                ) from e
            except Exception as e:
                raise RuntimeError(f"OCR 模板加载失败: {type(e).__name__}: {e}") from e
        self._coord_reader = CoordReader(
            mumu=self._mumu, ocr=self._template_ocr, roi_norm=roi
        )
        return self._coord_reader

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 顶部说明
        tip = QLabel(
            "前提: character_pos / block_size / view_area 三者准确.\n"
            "几何: 地图相对屏幕顺时针 45°, 4 个屏幕方位各对应 1 个地图维度:\n"
            "  · 走到屏幕**左**端 (玩家偏左, dx<0) → 解 mh\n"
            "  · 走到屏幕**右**端 (玩家偏右, dx>0) → 解 mw\n"
            "  · 走到屏幕**下**端 (玩家偏下, dy>0) → 解 mw+mh (独立 sum 字段)\n"
            "  · 走到屏幕**上**端 (玩家偏上, dy<0) → 无信息 (公式不含 mw/mh)\n"
            "应用时三个维度独立: 每个维度本次有反解就填回对应 spinbox, 没反解的不动."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #555; font-size: 11px;")
        root.addWidget(tip)

        # ---- 上下文展示 (锁定的地图 / 视野 / 几何参数) ----
        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 4)
        form.addRow("当前地图:", QLabel(self._map_label))
        form.addRow(
            "视野档位:",
            QLabel(self._vision_name if self._vision_spec else "(未配置)"),
        )

        cp = self._movement_profile.character_pos
        form.addRow("character_pos:", QLabel(f"({cp[0]:.4f}, {cp[1]:.4f})"))

        if self._vision_spec is not None:
            bw, bh = self._vision_spec.block_size
            form.addRow("block_size:", QLabel(f"({bw:.4f}, {bh:.4f})"))
        else:
            form.addRow("block_size:", QLabel("(视野档位未配置, 无法反解)"))

        if self._view_area is not None:
            vx0, vy0, vx1, vy1 = self._view_area
            form.addRow(
                "view_area:",
                QLabel(f"({vx0:.4f}, {vy0:.4f}, {vx1:.4f}, {vy1:.4f})"),
            )
        else:
            form.addRow("view_area:", QLabel("(未配置, 无法反解)"))
        root.addLayout(form)

        # ---- 反解状态 ----
        state_box = QLabel("")
        state_box.setStyleSheet(
            "background: #f5f5f5; border: 1px solid #ddd; "
            "padding: 8px; font-family: monospace;"
        )
        state_box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._state_label = state_box
        root.addWidget(self._state_label)

        # ---- 操作按钮 ----
        btn_row = QHBoxLayout()
        self._btn_obs = QPushButton("取一次观测")
        self._btn_obs.setMinimumHeight(36)
        self._btn_obs.clicked.connect(self._on_observe)
        btn_row.addWidget(self._btn_obs)

        btn_clear = QPushButton("清空所有")
        btn_clear.clicked.connect(self._on_clear)
        btn_row.addWidget(btn_clear)

        btn_apply = QPushButton("应用到主对话框")
        btn_apply.setMinimumHeight(36)
        btn_apply.clicked.connect(self._on_apply)
        btn_row.addWidget(btn_apply)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        root.addLayout(btn_row)

        # ---- 观测历史 ----
        root.addWidget(QLabel("观测历史:"))
        self._history_list = QListWidget()
        self._history_list.setStyleSheet("font-family: monospace; font-size: 11px;")
        root.addWidget(self._history_list, 1)

        # 依赖缺失则禁用观测按钮
        if self._vision_spec is None or self._view_area is None:
            self._btn_obs.setEnabled(False)
            tip2 = QLabel(
                "⚠ 依赖参数缺失: 请先在「运动配置」里补全 view_area + 当前视野的 block_size."
            )
            tip2.setStyleSheet("color: #c00; font-weight: bold;")
            root.addWidget(tip2)

    def _refresh_state_display(self) -> None:
        """
        显示规则 (三个维度独立, 不再做"未观测填 999"那种回退):
          - 有 latest (本次反解): 显示浮点 + 四舍五入值 + "(本次反解)"
          - 没 latest 但有 initial: 显示 initial + "(主对话框初值)"
          - 都没有: 显示 "未观测"
        各维度独立, 不互相推算. 想要"E + S 推 mh"这种, 手动算或多走一次 W.
        """

        def _line(latest: Optional[float], initial: Optional[int], label: str) -> str:
            if latest is not None:
                return (
                    f"{label} = {latest:.3f} (round → {int(round(latest))})  [本次反解]"
                )
            if initial is not None:
                return f"{label} = {initial}  [主对话框初值, 不会被应用]"
            return f"{label} = —  [未观测]"

        lines = [
            _line(self._mw_latest, self._initial_mw, "mw"),
            _line(self._mh_latest, self._initial_mh, "mh"),
            _line(self._mw_plus_mh_latest, self._initial_sum, "w+h (sum)"),
        ]

        # 如果三个本次反解都有, 显示一致性检查
        if (
            self._mw_latest is not None
            and self._mh_latest is not None
            and self._mw_plus_mh_latest is not None
        ):
            expected = self._mw_latest + self._mh_latest
            diff = self._mw_plus_mh_latest - expected
            tag = "✓ 一致" if abs(diff) <= 2 else "⚠ 偏差较大"
            lines.append(
                f"一致性: mw+mh={expected:.2f} vs sum={self._mw_plus_mh_latest:.2f} "
                f"(Δ={diff:+.2f}) {tag}"
            )

        lines.append(f"观测次数: {len(self._observations)}")
        self._state_label.setText("\n".join(lines))

    # -------------------------------------------------------------------------
    # 收集本次 outcome: 每个维度独立, "本次反解"才会进 outcome, 初值不会.
    # 给 _on_apply 调用.
    # -------------------------------------------------------------------------

    def _build_outcome(self) -> MapSizeSolverOutcome:
        out = MapSizeSolverOutcome()
        if self._mw_latest is not None:
            v = int(round(self._mw_latest))
            if 1 <= v <= 999:
                out.mw = v
        if self._mh_latest is not None:
            v = int(round(self._mh_latest))
            if 1 <= v <= 999:
                out.mh = v
        if self._mw_plus_mh_latest is not None:
            v = int(round(self._mw_plus_mh_latest))
            if 1 <= v <= 1998:
                out.sum_ = v
        return out

    # -------------------------------------------------------------------------
    # 取观测
    # -------------------------------------------------------------------------

    def _on_observe(self) -> None:
        if self._vision_spec is None or self._view_area is None:
            return  # 按钮已禁用, 兜底

        # 截一帧 + 同帧 OCR
        try:
            img = self._mumu.capture_window()
        except Exception as e:
            log.exception("截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return
        try:
            reader = self._get_coord_reader()
        except RuntimeError as e:
            QMessageBox.warning(self, "OCR 不可用", str(e))
            return
        try:
            coord, raw_text, _ = reader.read_verbose(image=img)
        except Exception as e:
            log.exception("OCR 调用异常")
            QMessageBox.critical(self, "OCR 调用异常", f"{type(e).__name__}: {e}")
            return
        if coord is None:
            QMessageBox.warning(
                self,
                "OCR 读不到坐标",
                f"OCR 拼出的原始字符串: {raw_text!r}\n请确认游戏小地图正在显示坐标.",
            )
            return
        gx, gy = coord
        log.info("反解 map_size: OCR=(%d, %d) on %s", gx, gy, self._map_label)

        self._pending = _PendingPick(pre_pos=(gx, gy))

        prompt = (
            f"当前观测: {self._map_label}  OCR=({gx},{gy})  视野={self._vision_name}    "
            f"请精确点击「角色脚下中心」"
        )
        if self._zoom_picker is None:
            self._zoom_picker = ZoomedSnapshotPicker(
                img,
                self._mumu,
                parent=self,
                show_recapture=False,  # OCR 已锁帧, 子窗换图会让 OCR 过期
                prompt=prompt,
            )
            self._zoom_picker.point_picked.connect(self._on_pick_in_zoom)
        else:
            self._zoom_picker.set_image(img)
            self._zoom_picker.set_prompt(prompt)

        self._zoom_picker.set_record_count(len(self._observations), None)
        self._zoom_picker.show()
        self._zoom_picker.raise_()
        self._zoom_picker.activateWindow()

    def _on_pick_in_zoom(self, nx: float, ny: float) -> None:
        if self._pending is None:
            log.warning("_on_pick_in_zoom 触发但 _pending 为空, 忽略")
            return
        if self._vision_spec is None or self._view_area is None:
            return

        screen_pos = (nx, ny)
        result = solve_map_size_observation(
            pre_pos=self._pending.pre_pos,
            screen_pos=screen_pos,
            view_area=self._view_area,
            block_size=self._vision_spec.block_size,
            character_pos=self._movement_profile.character_pos,
        )

        # 累积 (取最新策略)
        if result.mw is not None:
            self._mw_latest = result.mw
        if result.mh is not None:
            self._mh_latest = result.mh
        if result.mw_plus_mh is not None:
            self._mw_plus_mh_latest = result.mw_plus_mh

        # 历史
        obs = _Observation(
            pre_pos=self._pending.pre_pos, screen_pos=screen_pos, result=result
        )
        self._observations.append(obs)
        self._append_history_item(obs)
        self._refresh_state_display()

    def _append_history_item(self, obs: _Observation) -> None:
        def _f(v: Optional[float]) -> str:
            return f"{v:.3f}" if v is not None else "—"

        head = (
            f"#{len(self._observations)} OCR=({obs.pre_pos[0]},{obs.pre_pos[1]}) "
            f"屏幕=({obs.screen_pos[0]:.4f},{obs.screen_pos[1]:.4f}) "
            f"方向={obs.result.direction}"
        )
        solved = (
            f"  解出: mw={_f(obs.result.mw)}  "
            f"mh={_f(obs.result.mh)}  "
            f"mw+mh={_f(obs.result.mw_plus_mh)}"
        )
        item_text = head + "\n" + solved
        for note in obs.result.notes:
            item_text += "\n  " + note
        item = QListWidgetItem(item_text)
        # 警告 (note 里有 ⚠) 标红; 此次观测什么都没解出 (none / 仅 N) 标灰.
        # has_any 检查涵盖了所有"无信息"情形, 包括 direction == "N" 或 "none".
        # X+Y 组合里只要有一个轴解出值 (例如 "W+N" 解出了 mh), 就算有信息, 不标灰.
        if any("⚠" in n for n in obs.result.notes):
            item.setForeground(QColor(180, 60, 60))
        elif not obs.result.has_any:
            item.setForeground(QColor(120, 120, 120))
        self._history_list.addItem(item)
        self._history_list.scrollToBottom()

    # -------------------------------------------------------------------------
    # 清空 / 应用
    # -------------------------------------------------------------------------

    def _on_clear(self) -> None:
        ans = QMessageBox.question(
            self,
            "确认清空",
            "清空当前累计候选 + 观测历史. 主对话框 spinbox 不会被改动.",
        )
        if ans != QMessageBox.Yes:
            return
        self._mw_latest = None
        self._mh_latest = None
        self._mw_plus_mh_latest = None
        self._observations.clear()
        self._history_list.clear()
        self._refresh_state_display()

    def _on_apply(self) -> None:
        """
        组装 MapSizeSolverOutcome 写回主对话框. 三个字段独立: 本次反解出哪个就
        填哪个, 没反解出来的字段保持空 (主对话框 spinbox 维持原值).
        """
        outcome = self._build_outcome()

        if not outcome.has_any:
            QMessageBox.warning(
                self,
                "无有效反解",
                "本次未反解出任何 mw / mh / sum.\n" "请先取观测 (走到屏幕边缘) 再应用.",
            )
            return

        # 越界检查 (_build_outcome 已过滤过, 这里再确认一次)
        for label, v, lo, hi in (
            ("mw", outcome.mw, 1, 999),
            ("mh", outcome.mh, 1, 999),
            ("sum", outcome.sum_, 1, 1998),
        ):
            if v is not None and not (lo <= v <= hi):
                QMessageBox.warning(
                    self,
                    "数值越界",
                    f"{label}={v} 不在 [{lo}, {hi}] 范围内, 拒绝应用. "
                    f"请重新观测或检查依赖参数.",
                )
                return

        # 一致性提示 (本次同时有 mw + mh + sum 时)
        consistency_warn = ""
        if (
            outcome.mw is not None
            and outcome.mh is not None
            and outcome.sum_ is not None
        ):
            derived = outcome.mw + outcome.mh
            if abs(derived - outcome.sum_) >= 2:
                consistency_warn = (
                    f"\n\n⚠ 一致性检查: mw+mh={derived} 与 sum={outcome.sum_} "
                    f"相差 {abs(derived - outcome.sum_)} 格. 仍将按反解结果应用, "
                    f"主对话框会同时存两份, runtime 以 map_size 为准."
                )

        parts = []
        if outcome.mw is not None:
            parts.append(f"mw = {outcome.mw}")
        if outcome.mh is not None:
            parts.append(f"mh = {outcome.mh}")
        if outcome.sum_ is not None:
            parts.append(f"sum = {outcome.sum_}")
        msg = (
            "将填回主对话框 spinbox (仅以下字段, 其它保持原值):\n  "
            + "\n  ".join(parts)
            + consistency_warn
            + "\n\n主对话框需要点「保存到 YAML」才会真正落盘. 确认?"
        )
        ans = QMessageBox.question(self, "确认应用", msg)
        if ans != QMessageBox.Yes:
            return

        self.accepted_outcome = outcome
        self.accept()

    # -------------------------------------------------------------------------
    # 关闭
    # -------------------------------------------------------------------------

    def closeEvent(self, ev) -> None:
        if self._zoom_picker is not None:
            self._zoom_picker.close()
        super().closeEvent(ev)
