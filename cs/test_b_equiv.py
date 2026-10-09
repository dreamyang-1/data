# B类等价判定单测：三向规则（类别后缀/行政区划后缀/地域前缀）
import sys
sys.path.insert(0, "/root/yyy/Oagnet")
from structured_binding import _vector_value_equivalent as eq

POS = [
    ("三级医院", "三级"), ("二级甲等医院", "二级甲等"),
    ("上海市", "上海"), ("上海", "上海市"),
    ("苏云品牌", "苏云"), ("北京洁安厂家", "北京洁安"),
    ("协和医院", "协和"), ("振德医疗品牌", "振德医疗"),
    ("苏云", "江苏苏云"), ("江苏苏云", "苏云"),           # 地域前缀双向
    ("上海振德医疗", "振德医疗"), ("云南白药", "白药"),
]
NEG = [
    ("心内科", "内科"), ("内科", "心内科"),
    ("心内科", "心血管内科"),                             # 缩略简称，留配置治理
    ("北京", "上海"), ("三级", "二级"),
    ("医院", ""), ("a医院", "a"), ("小儿外科", "外科"),
]
fails = 0
for s, c in POS:
    if not (eq(s, c) and eq(c, s)): print("应等价却判否:", s, "|", c); fails += 1
for s, c in NEG:
    if eq(s, c) or eq(c, s): print("应不等价却判等:", s, "|", c); fails += 1
print("用例", len(POS)+len(NEG), "失败", fails)
