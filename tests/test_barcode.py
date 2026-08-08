"""标签二维码解码单测：只取标签大码(含 (S) 序列号)，芯片小数字码忽略。"""
import os

from app.recognition.barcode import parse_label_text, decode_labels, _is_label_code

_SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples", "region_front.png")


def test_parse_label_fields():
    s = "(L)64GB 2Rx4 PC5-5600B-RA0-1010-XT(S)80CE02253633436BCE(P)M321R8GA0EB2-CWMXH(M)S00O0Q0"
    d = parse_label_text(s)
    assert d["sn"] == "80CE02253633436BCE"
    assert d["model"] == "M321R8GA0EB2-CWMXH"
    assert d["capacity"] == "64GB"
    assert d["frequency"] == "DDR5-5600"           # PC5-5600 → DDR5-5600


def test_is_label_code_filters_chip_codes():
    assert _is_label_code("(S)80CE02253633436BCE(P)X") is True
    assert _is_label_code("1478844965340801") is False    # 芯片小数字码 → 不是标签码


def test_decode_sample_label_only():
    """从样例图解码：只出标签大码(SN=80CE...)，不含芯片纯数字码。"""
    if not os.path.exists(_SAMPLE):
        import pytest
        pytest.skip("样例图缺失")
    codes = decode_labels(_SAMPLE)
    sns = [c["sn"] for c in codes]
    assert "80CE02253633436BCE" in sns              # 标签大码解出、SN 精确
    assert all(c["sn"] for c in codes)              # 返回的都是标签码(有 SN)，芯片码已滤掉
