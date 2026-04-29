"""装备精炼序列查看对话框.

读 ``config/refine_logs/<装备名>.yaml``, 展示某件装备的全部精炼记录:

    UI 布局:
        ┌─顶部─ 装备选择 + 刷新 + 打开文件夹 + 统计总数
        ├─左────────┬─右────────────
        │ 序列列表   │ 详细信息
        │ (QListW)  │ (QTextEdit)
        └───────────┴──────────────

    每条序列项: "#N | 位置 X | 新词条名 数值 ← 旧词条名 数值"
    选中项后右侧展示完整详情 (时间/决定/基础属性/旧词条全列表/新词条).

不依赖 RefineProfile, 直接读 yaml — 这样即使配置文件还没加载也能看历史数据.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


# =============================================================================
# 数据
# =============================================================================


@dataclass(frozen=True)
class _RecordLite:
    """轻量化的记录视图; 只在 viewer 内用, 不依赖 RefineRecord."""

    refine_no: int
    timestamp: str
    base_attrs: dict
    attrs_before: list[dict]
    new_attr: dict
    replace_index: int
    decision: str

    @classmethod
    def from_dict(cls, d: dict) -> "_RecordLite":
        return cls(
            refine_no=int(d.get("refine_no", 0)),
            timestamp=str(d.get("timestamp", "")),
            base_attrs=dict(d.get("base_attrs", {})),
            attrs_before=list(d.get("attrs_before", [])),
            new_attr=dict(d.get("new_attr", {})),
            replace_index=int(d.get("replace_index", -1)),
            decision=str(d.get("decision", "")),
        )


def _fmt_attr(d: dict) -> str:
    """属性 dict → '攻击 126' / '免伤 2.2%' 这种短文本."""
    if not d:
        return "(空)"
    name = d.get("name", "?")
    value = d.get("value", 0)
    unit = d.get("unit", "")
    # value 可能是 int 也可能是 float; 整数显示成整数
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{name} {value}{unit}"


def _list_log_files(log_dir: Path) -> list[Path]:
    """列出 refine_logs 目录下的 .yaml 文件 (排除 .bak / 隐藏文件)."""
    if not log_dir.exists():
        return []
    out: list[Path] = []
    for p in sorted(log_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix != ".yaml":
            continue
        if p.name.startswith("."):
            continue
        out.append(p)
    return out


def _load_log_file(path: Path) -> tuple[str, list[_RecordLite]]:
    """读 yaml. 返回 (装备名, 记录列表). 失败时抛异常."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    eq_name = str(data.get("equipment_name", path.stem))
    records_raw = data.get("records", []) or []
    records = [_RecordLite.from_dict(r) for r in records_raw]
    # 按 refine_no 排序 (yaml 里通常已经有序, 防御性地再排一次)
    records.sort(key=lambda r: r.refine_no)
    return eq_name, records


# =============================================================================
# Dialog
# =============================================================================


class RefineLogViewerDialog(QDialog):
    def __init__(self, log_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("装备精炼序列查看")
        self.resize(1100, 720)
        self._log_dir = Path(log_dir)
        # 当前装备的记录缓存 (按列表行号索引)
        self._records: list[_RecordLite] = []
        self._build_ui()
        self._reload_equipment_list()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # 顶部
        top = QHBoxLayout()
        top.addWidget(QLabel("装备:"))
        self._eq_combo = QComboBox()
        self._eq_combo.setMinimumWidth(220)
        self._eq_combo.currentIndexChanged.connect(self._on_eq_changed)
        top.addWidget(self._eq_combo)

        b_reload = QPushButton("刷新")
        b_reload.setToolTip("重新扫描 refine_logs 目录, 读取最新数据")
        b_reload.clicked.connect(self._reload_equipment_list)
        top.addWidget(b_reload)

        b_open_dir = QPushButton("打开文件夹")
        b_open_dir.setToolTip("在文件管理器里打开 refine_logs 目录, 方便手动查看 yaml")
        b_open_dir.clicked.connect(self._open_log_dir)
        top.addWidget(b_open_dir)

        top.addStretch(1)
        self._lbl_summary = QLabel("总记录数: 0")
        self._lbl_summary.setStyleSheet("color: #555;")
        top.addWidget(self._lbl_summary)
        root.addLayout(top)

        # 路径提示
        path_lbl = QLabel(f"数据目录: {self._log_dir}")
        path_lbl.setStyleSheet("color: #777; font-size: 11px;")
        path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(path_lbl)

        # 主体: 左序列列表 + 右详情
        splitter = QSplitter(Qt.Horizontal)

        # 左侧
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("精炼序列 (选中查看详情):"))
        self._list = QListWidget()
        # 等宽字体, 让对齐好看
        font = QFont("Consolas, Menlo, Courier New, monospace")
        font.setStyleHint(QFont.Monospace)
        self._list.setFont(font)
        self._list.itemSelectionChanged.connect(self._on_record_selected)
        left_layout.addWidget(self._list)
        splitter.addWidget(left)

        # 右侧
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("详细信息:"))
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        font2 = QFont("Consolas, Menlo, Courier New, monospace")
        font2.setStyleHint(QFont.Monospace)
        self._detail.setFont(font2)
        right_layout.addWidget(self._detail)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        root.addWidget(splitter, 1)

        # 底部关闭
        bot = QHBoxLayout()
        bot.addStretch(1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.close)
        bot.addWidget(b_close)
        root.addLayout(bot)

    # =========================================================================
    # 数据
    # =========================================================================

    def _reload_equipment_list(self) -> None:
        """扫 refine_logs 目录, 重建装备下拉框."""
        prev_eq = self._eq_combo.currentText() if self._eq_combo.count() else ""
        self._eq_combo.blockSignals(True)
        self._eq_combo.clear()
        files = _list_log_files(self._log_dir)
        # 显示文本: 装备名 (取自 yaml 内 equipment_name 字段, 兜底用文件名 stem)
        # 用户数据: 文件路径
        for path in files:
            try:
                eq_name, recs = _load_log_file(path)
            except Exception as e:
                log.exception("读 %s 失败", path)
                eq_name = path.stem
                recs = []
            label = f"{eq_name}  ({len(recs)} 条)"
            self._eq_combo.addItem(label, userData=path)
        self._eq_combo.blockSignals(False)

        if self._eq_combo.count() == 0:
            self._lbl_summary.setText("总记录数: 0  (refine_logs 目录为空)")
            self._records = []
            self._list.clear()
            self._detail.setPlainText(
                "尚无任何采集记录.\n\n"
                "数据会在你使用 [开始采集] 后自动写入: " + str(self._log_dir)
            )
            return

        # 优先恢复之前选的
        if prev_eq:
            for i in range(self._eq_combo.count()):
                if self._eq_combo.itemText(i).startswith(prev_eq + " "):
                    self._eq_combo.setCurrentIndex(i)
                    break
            else:
                self._eq_combo.setCurrentIndex(0)
        else:
            self._eq_combo.setCurrentIndex(0)
        self._on_eq_changed()

    def _on_eq_changed(self) -> None:
        path = self._eq_combo.currentData()
        if not path:
            self._records = []
            self._list.clear()
            return
        try:
            eq_name, recs = _load_log_file(path)
        except Exception as e:
            log.exception("读 %s 失败", path)
            QMessageBox.critical(
                self, "读取失败", f"读 {path.name} 失败:\n{type(e).__name__}: {e}"
            )
            self._records = []
            self._list.clear()
            return
        self._records = recs
        self._lbl_summary.setText(f"总记录数: {len(recs)}  (装备: {eq_name})")
        self._populate_list(recs)
        # 默认选中第一条
        if self._list.count():
            self._list.setCurrentRow(0)
        else:
            self._detail.setPlainText("(该装备尚无记录)")

    def _populate_list(self, recs: list[_RecordLite]) -> None:
        self._list.clear()
        # 计算各列宽度: refine_no 几位, 位置 1 位, 新词条文本最长几字, 这样列才整齐
        max_no = max((r.refine_no for r in recs), default=1)
        no_width = max(2, len(str(max_no)))
        new_attr_strs = [_fmt_attr(r.new_attr) for r in recs]
        max_new_w = max((len(s) for s in new_attr_strs), default=8)

        for r in recs:
            new_text = _fmt_attr(r.new_attr)
            # 旧词条 (被替换的那条)
            replaced_text = "?"
            if 0 <= r.replace_index < len(r.attrs_before):
                replaced_text = _fmt_attr(r.attrs_before[r.replace_index])
            # 位置 (1-based; 跟用户在游戏里看到的"第几行"对应)
            pos_str = str(r.replace_index + 1) if r.replace_index >= 0 else "?"
            # decision 标记: 接受 = ✔, 取消 = ✗, 其他 = ?
            mark = {"accepted": "✔", "cancelled": "✗"}.get(r.decision, "?")
            line = (
                f"#{r.refine_no:>{no_width}}  "
                f"位置 {pos_str}  "
                f"{new_text:<{max_new_w}}  "
                f"← 替换 [{replaced_text}]  {mark}"
            )
            item = QListWidgetItem(line)
            self._list.addItem(item)

    def _on_record_selected(self) -> None:
        row = self._list.currentRow()
        if not (0 <= row < len(self._records)):
            self._detail.setPlainText("")
            return
        r = self._records[row]
        self._detail.setPlainText(self._format_detail(r))

    def _format_detail(self, r: _RecordLite) -> str:
        lines: list[str] = []
        lines.append(f"采集次数 (refine_no): {r.refine_no}")
        lines.append(f"采集时间:             {r.timestamp}")
        decision_zh = {
            "accepted": "✔ 已接受",
            "cancelled": "✗ 已取消",
        }.get(r.decision, r.decision)
        lines.append(f"决定:                 {decision_zh}")
        lines.append("")

        lines.append("基础属性:")
        if r.base_attrs:
            for k, v in r.base_attrs.items():
                v_disp = int(v) if isinstance(v, float) and v.is_integer() else v
                lines.append(f"   {k}: {v_disp}")
        else:
            lines.append("   (无)")
        lines.append("")

        lines.append("旧词条 (本次精炼前):")
        if r.attrs_before:
            for i, a in enumerate(r.attrs_before):
                marker = "  ← 被替换" if i == r.replace_index else ""
                lines.append(f"   {i + 1}. {_fmt_attr(a)}{marker}")
        else:
            lines.append("   (无)")
        lines.append("")

        lines.append("新词条 (本次精炼结果):")
        lines.append(f"   {_fmt_attr(r.new_attr)}")
        if 0 <= r.replace_index < len(r.attrs_before):
            old = r.attrs_before[r.replace_index]
            old_value = old.get("value", 0)
            new_value = r.new_attr.get("value", 0)
            same_name = old.get("name") == r.new_attr.get("name")
            same_unit = old.get("unit", "") == r.new_attr.get("unit", "")
            if same_name and same_unit:
                # 同名同单位 → 直接给数值差
                try:
                    diff = float(new_value) - float(old_value)
                    sign = "+" if diff >= 0 else ""
                    lines.append(
                        f"   (同名替换, 数值差: {sign}{diff:g}{r.new_attr.get('unit', '')})"
                    )
                except (TypeError, ValueError):
                    pass

        return "\n".join(lines)

    def _open_log_dir(self) -> None:
        """在文件管理器里打开 log 目录."""
        if not self._log_dir.exists():
            QMessageBox.information(
                self,
                "目录不存在",
                f"目录还没创建:\n{self._log_dir}\n\n采集开始后会自动创建.",
            )
            return
        try:
            if sys.platform.startswith("win"):
                # 用 explorer 打开 (PowerShell start 也行, 但 explorer 更直接)
                subprocess.Popen(["explorer", str(self._log_dir)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self._log_dir)])
            else:
                subprocess.Popen(["xdg-open", str(self._log_dir)])
        except Exception as e:
            log.exception("打开目录失败")
            QMessageBox.warning(
                self, "打开失败", f"{type(e).__name__}: {e}\n路径: {self._log_dir}"
            )
