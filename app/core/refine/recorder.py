"""
yaml 持久化: 一件装备一个 yaml 文件.

文件格式:
    equipment_name: 呼如木甲
    created_at: 2026-04-29T14:50:00
    records:
      - refine_no: 1
        timestamp: 2026-04-29T15:06:23
        base_attrs: {防御: 3135, 罡气: 61}
        attrs_before:
          - {name: 攻击, value: 126, unit: ""}
          - {name: 免伤, value: 2.2, unit: "%"}
          - {name: 闪避, value: 58,  unit: ""}
        new_attr: {name: 招架, value: 33, unit: ""}
        replace_index: 1
        decision: cancelled
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .data import ConfirmPanelState, RefineRecord

log = logging.getLogger(__name__)


class RefineRecorder:
    """一件装备一个 yaml.

    所有写入都会:
        - 按 refine_no 排序
        - 防止 refine_no 重复 (同 refine_no 后写覆盖前者并打印 warning)

    线程安全: 单线程内顺序调用即可; runner 跑在 QThread 里, 不会和 GUI 线程
    并发写同一份 yaml.
    """

    def __init__(self, log_path: Path, equipment_name: Optional[str] = None) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._equipment_name = equipment_name

    # ---------- 读 ----------

    def load_data(self) -> dict:
        if not self.log_path.exists():
            return {}
        return yaml.safe_load(self.log_path.read_text(encoding="utf-8")) or {}

    def load_all(self) -> list[RefineRecord]:
        data = self.load_data()
        records = data.get("records", []) or []
        return [RefineRecord.from_dict(r) for r in records]

    def next_refine_no(self) -> int:
        """返回下一个待写入的 refine_no (历史最大值 + 1, 没记录返回 1)."""
        records = self.load_all()
        if not records:
            return 1
        return max(r.refine_no for r in records) + 1

    # ---------- 写 ----------

    def append_from_confirm(
        self,
        state: ConfirmPanelState,
        refine_no: int,
        decision: str = "cancelled",
    ) -> RefineRecord:
        """从 ConfirmPanelState 构造 RefineRecord 并落盘."""
        if state.new_attr is None:
            raise ValueError("ConfirmPanelState.new_attr 不能为空")
        rec = RefineRecord(
            refine_no=refine_no,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            base_attrs={k: float(v) for k, v in state.base_attrs.items()},
            attrs_before=[a.to_dict() for a in state.extra_attrs_before],
            new_attr=state.new_attr.to_dict(),
            replace_index=state.replace_index,
            decision=decision,
        )
        self.append_record(state.equipment_name, rec)
        return rec

    def append_record(self, equipment_name: str, rec: RefineRecord) -> None:
        data = self.load_data()
        data.setdefault("equipment_name", equipment_name)
        data.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        records: list = data.setdefault("records", [])

        # 去重: 同 refine_no 已存在则覆盖
        existing_idx: Optional[int] = None
        for i, r in enumerate(records):
            if int(r.get("refine_no", -1)) == rec.refine_no:
                existing_idx = i
                break
        if existing_idx is not None:
            log.warning(
                "refine_no=%d 已存在于 %s, 覆盖旧记录", rec.refine_no, self.log_path
            )
            records[existing_idx] = rec.to_dict()
        else:
            records.append(rec.to_dict())
        records.sort(key=lambda r: int(r.get("refine_no", 0)))

        # 备份后写入
        if self.log_path.exists():
            backup = self.log_path.with_suffix(self.log_path.suffix + ".bak")
            try:
                backup.write_bytes(self.log_path.read_bytes())
            except OSError:
                log.exception("备份 %s 失败 (继续写主文件)", backup)

        self.log_path.write_text(
            yaml.safe_dump(
                data,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
