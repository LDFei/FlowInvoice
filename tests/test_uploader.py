# tests/test_uploader.py —— 上传解析器（跨业务复用核心）
from pathlib import Path

from app.core.uploader import InvoiceInput, Uploader


def test_save_and_parse_returns_invoice_input(tmp_path):
    # 作用：文件落盘 → 标准 DTO，字段原样透传
    src = tmp_path / "ticket.txt"
    src.write_text("票面", encoding="utf-8")
    with src.open("rb") as f:
        dto = Uploader().save_and_parse(
            f, "ticket.txt",
            direction="travel", declared_amount=100.0, employee_id="1001",
        )
    assert dto.direction == "travel"
    assert dto.declared_amount == 100.0
    assert dto.employee_id == "1001"
    assert dto.payment_method == "personal"  # 默认值
    # 作用：落盘文件确实存在且可读
    assert Path(dto.file_path).read_text(encoding="utf-8") == "票面"


def test_to_dict_shape():
    # 作用：DTO → dict 的键与 State 预期一致（业务模块只消费这些字段）
    d = InvoiceInput(file_path="/tmp/x", direction="travel").to_dict()
    assert set(d) == {"file_path", "direction", "purpose", "declared_amount",
                      "payment_method", "employee_id", "app_id"}
