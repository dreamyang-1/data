# 生成 laya 微调训练草稿：意图分类 + 追问判定
# 词表取自语义描述文件.md 中国药语义模型最新版本（医药销售域）的指标/实体/维度，
# 问法和追问话术参考实际业务问题.md 里的真实用户提问
# 用法: /root/venvs/laya-poc/bin/python gen_train_draft.py
import json
import random
import re
from collections import Counter
from pathlib import Path

random.seed(20261014)

HERE = Path(__file__).parent

# 测试集里已有的原句不进训练集，保证评估可信
_test = json.loads((HERE / "laya_poc_dataset.json").read_text(encoding="utf-8"))
TEST_INTENT_TEXTS = set(x["text"] for x in _test["intent"])
TEST_FOLLOWUP_TEXTS = set(x["text"] for x in _test["followup"])
TEST_FOLLOWUP_PAIRS = set((x["context"], x["text"]) for x in _test["followup"])

# ---- 词表：语义建模文件（医药销售域）----

# 指标 + 口径同义词（含税销售总额的同义说法最常用）
METRICS = ["含税销售总额", "不含税销售总额", "销售总成本", "销售总数量", "订单笔数",
           "已合作经销商数", "区域全部医院总数", "医院下属科室数量", "有效订单去重医院数",
           "区域医院覆盖率", "平均每单销售金额"]
METRIC_COLLO = ["销售额", "销售总额", "销量", "销售数量", "订单数", "单据数量", "总成本",
                "无税销售额", "每单平均金额", "医院覆盖率", "经销商覆盖", "业务规模"]
# 语义模型里分析维度含渠道业态的指标，业态过滤只用于这些
YETAI_METRICS = ["含税销售总额", "不含税销售总额", "销售总成本", "销售总数量", "已合作经销商数",
                 "有效订单去重医院数", "区域医院覆盖率", "平均每单销售金额"]
ENTITIES = ["经销商", "医院", "业务员", "销售代表", "商品", "产品", "生产厂家", "产品分类",
            "品类", "产品线", "销售公司", "国药公司", "科室"]
HOSPITAL_LEVELS = ["三级医院", "二级医院", "一级医院", "未定级医院"]
BIZ_TYPES = ["直销", "分销", "集中配送", "产品配送", "物流平台", "零售"]
PROVINCES = ["上海市", "江苏省", "北京市", "浙江省", "广东省", "四川省", "安徽省",
             "重庆", "山东省", "河南省"]
TIMES = ["今年", "去年", "2025年", "2024年", "近三个月", "上个月", "本季度", "近半年", "最近12个月"]

# 真实商品名（来自实际业务问题.md）
PRODUCTS = ["空心纤维血液透析器", "一次性使用静脉留置针", "一次性使用血液透析滤过器及配套管路",
            "外周插管中心静脉导管", "正压接头", "一次性使用泵用注射器 带针", "血液净化体外循环血路",
            "绝对计数管", "一次性使用灭菌橡胶外科手套", "植入式给药装置专用针"]

# 真实经销商名（来自实际业务问题.md）
DEALERS = ["国药集团上海医疗器械有限公司", "杭州琅骏医疗科技有限公司", "上海福荫商贸有限公司",
           "上海鸿迪医疗科技有限公司", "上海泰初臻医疗科技有限公司", "重庆友驰医疗器械有限公司",
           "南京百奥生物科技有限公司", "上海胥宇医疗器械有限公司", "浙江大佳医疗器械有限公司",
           "南京浩盈商贸有限公司", "江苏卓尔医疗器械有限公司"]

NUMS = ["3", "5", "6", "8", "10", "20"]

# 真实基础问句，用作追问的上文（来自实际业务问题.md）
BASE_QS = [
    "查询空心纤维血液透析器产品合作的经销商名单",
    "按月看销售额。",
    "统计上海市各个经销商的区域医院覆盖率",
    "查询2025年江苏省订单笔数",
    "统计各经销商的含税销售总额",
    "查一下今年各产品的销量",
    "统计上海医院覆盖率",
    "查询各经销商合作时长",
    "把各等级的含税销售总额、销售总数量和订单笔数汇总给我。",
    "查询含税销售总额排名前10的医院，显示医院名称、医院等级和订单笔数。",
    "一次性使用静脉留置针在各省卖得怎么样？",
    "查询每个经销商合作时长",
]


def pick(*pools):
    return [random.choice(p) for p in pools]


def sample_until(templates, n, label, existed, max_attempts=8000):
    """轮询模板凑够 n 条不重复样本，组合数不足时靠尝试上限兜底。"""
    out, attempts = [], 0
    while len(out) < n and attempts < max_attempts:
        text = random.choice(templates)()
        attempts += 1
        if (text, label) not in existed and text not in TEST_INTENT_TEXTS:
            existed.add((text, label))
            out.append({"text": text, "label": label})
    if len(out) < n:
        print("  [warn] %s only got %d/%d unique samples" % (label, len(out), n))
    return out


def gen_intent():
    existed = set()
    plans = {
        "指标查询": 50, "明细查询": 50, "趋势分析": 50, "对比分析": 50, "占比分析": 50,
        "异常分析": 50, "归因分析": 50, "预测分析": 50, "报表生成": 50, "解释口径": 40,
        "闲聊": 50, "非数据任务": 50,
    }
    m = lambda: random.choice(METRICS + METRIC_COLLO)
    tpls = {
        "指标查询": [
            lambda: "查询%s的%s。" % (pick(DEALERS)[0], pick(METRICS + METRIC_COLLO)[0]),
            lambda: "%s一共卖了多少钱？" % pick(PRODUCTS)[0],
            lambda: "统计%s%s" % (pick(PROVINCES)[0], pick(["医院覆盖率", "订单笔数", "销售额", "已合作经销商数"])),
            lambda: "查询%s%s%s" % (pick(TIMES)[0], pick(PROVINCES)[0], pick(METRICS)[0]),
            lambda: "%s的%s是多少" % (pick(DEALERS + PROVINCES)[0], m()),
            lambda: "老板要看%s的销售数据" % pick(PRODUCTS)[0],
            lambda: "统计%s%s的%s" % (pick(PROVINCES)[0], pick(HOSPITAL_LEVELS)[0], pick(METRICS + METRIC_COLLO)[0]),
            lambda: "%s%s业态的%s" % (pick(TIMES)[0], pick(BIZ_TYPES)[0], pick(YETAI_METRICS)[0]),
            lambda: "%s的%s" % (pick(TIMES)[0], pick(METRICS + METRIC_COLLO)[0]),
        ],
        "明细查询": [
            lambda: "查询%s产品合作的经销商名单。" % pick(PRODUCTS)[0],
            lambda: "给出具体的销售订单明细",
            lambda: "查询商品品类为注射剂的商品基础明细",
            lambda: "列出合作时长大于%s个月的经销商名单。" % random.choice(NUMS),
            lambda: "查询订单笔数大于%s笔的经销商名单" % random.choice(NUMS),
            lambda: "%s的货主要供到哪些省份？" % pick(PRODUCTS)[0],
            lambda: "请提供%s使用科室。" % pick(PRODUCTS)[0],
            lambda: "查一下%s的订单明细" % pick(DEALERS)[0],
        ],
        "趋势分析": [
            lambda: "按月看%s。" % m(),
            lambda: "分析%s%s的月度趋势" % (pick(TIMES)[0], m()),
            lambda: "%s近半年是涨还是跌" % m(),
            lambda: "对比一下全部产品的月度%s趋势。" % pick(METRICS + METRIC_COLLO)[0],
            lambda: "%s的%s走势怎么样" % (pick(PROVINCES)[0], m()),
        ],
        "对比分析": [
            lambda: "对比%s和%s%s的%s" % (pick(TIMES)[0], random.choice(["今年", "去年", "上个月", "本月"]), pick(PROVINCES)[0], m()),
            lambda: "%s和%s的%s哪个高" % (pick(DEALERS)[0], pick(DEALERS)[0], m()),
            lambda: "对比一下%s和%s年的%s" % (pick(["2024", "2025", "2023"])[0], pick(["2024", "2025", "2023"])[0], m()),
            lambda: "今年和去年%s%s差距多大" % (pick(PROVINCES)[0], m()),
            lambda: "%s和%s两个品牌的%s对比一下" % (pick(PRODUCTS)[0], pick(PRODUCTS)[0], m()),
        ],
        "占比分析": [
            lambda: "各%s的%s占比是多少" % (pick(ENTITIES)[0], m()),
            lambda: "%s占整体%s的比例" % (pick(PRODUCTS + PROVINCES)[0], m()),
            lambda: "%s对总%s的贡献占比" % (pick(DEALERS)[0], pick(METRICS)[0]),
            lambda: "各渠道业态的订单金额分布",
            lambda: "%s在各省份的销售订单分布如何" % pick(PRODUCTS)[0],
        ],
        "异常分析": [
            lambda: "找出%s%s下降异常的%s" % (pick(TIMES)[0], m(), pick(ENTITIES)[0]),
            lambda: "哪些%s的%s波动比较大" % (pick(ENTITIES)[0], m()),
            lambda: "排查%s%s的异常点" % (pick(TIMES)[0], m()),
            lambda: "%s里有没有%s明显偏低的%s" % (pick(TIMES)[0], m(), pick(["经销商", "医院", "产品"])[0]),
        ],
        "归因分析": [
            lambda: "为什么%s%s下滑了" % (pick(TIMES)[0], m()),
            lambda: "%s%s降低的原因是什么" % (pick(DEALERS + PROVINCES)[0], m()),
            lambda: "分析%s%s增长主要靠什么" % (pick(TIMES)[0], m()),
            lambda: "%s上涨背后是什么因素驱动的" % m(),
        ],
        "预测分析": [
            lambda: "预测下个季度%s" % m(),
            lambda: "估计%s%s%s会到多少" % (random.choice(["今年下半年", "明年", "下个月"]), pick(DEALERS)[0], m()),
            lambda: "%s未来%s个月走势预期如何" % (m(), random.choice(NUMS)),
            lambda: "按目前节奏%s%s能完成全年目标吗" % (pick(DEALERS)[0], m()),
        ],
        "报表生成": [
            lambda: "生成%s销售月报" % pick(TIMES)[0],
            lambda: "帮我出一份%s%s的经营周报" % (pick(PROVINCES)[0], pick(TIMES)[0]),
            lambda: "把%s%s汇总做成日报" % (pick(TIMES)[0], m()),
            lambda: "%s%s做个汇总报告发我" % (pick(PROVINCES)[0], pick(TIMES)[0]),
        ],
        "解释口径": [
            lambda: "%s这个指标是怎么定义的" % pick(METRICS)[0],
            lambda: "%s的计算公式是什么" % pick(METRICS)[0],
            lambda: "%s的统计口径包含哪些数据" % pick(METRICS)[0],
            lambda: "合作时长是怎么算的",
            lambda: "医院覆盖率是怎么算的",
            lambda: "默认查询多久的数据",
        ],
    }

    rows = []
    for label, n in plans.items():
        if label == "闲聊":
            chats = ["你好", "在吗", "早上好", "下午好", "辛苦了", "谢谢你", "你是谁", "你能做什么", "新年快乐", "周末愉快",
                     "哈哈", "好的", "明白了", "OK", "收到", "拜拜", "再见", "晚安", "今天天气不错", "吃午饭去",
                     "嘿嘿", "嗯嗯", "好嘞", "多谢", "打扰了", "你好呀", "您好", "hi", "hello", "在不在",
                     "谢谢啦", "辛苦辛苦", "不错不错", "可以呀", "行吧", "懂了", "原来如此", "学到了", "厉害", "支持下",
                     "领导好", "好久不见", "最近咋样", "下班啦", "午安", "早啊", "晚上好", "节日快乐", "????", "test"]
            for c in chats:
                if len([r for r in rows if r["label"] == label]) >= n:
                    break
                if (c, label) not in existed and c not in TEST_INTENT_TEXTS:
                    existed.add((c, label))
                    rows.append({"text": c, "label": label})
        elif label == "非数据任务":
            nons = ["讲个笑话听听", "写一首关于秋天的诗", "推荐几部好看的电影", "帮我翻译一段英文", "写一份活动策划文案",
                    "用Python写个爬虫", "帮我写封请假邮件", "故宫门票多少钱", "苹果醋怎么喝", "今晚有什么英超比赛",
                    "帮我算下个人所得税", "怎么做红烧肉", "推荐几本管理学的书", "给我讲个鬼故事",
                    "帮我起个公司名字", "写一段生日祝福", "分析一下这份简历", "帮我做个PPT大纲", "周杰伦的新专辑",
                    "附近有什么好吃的餐厅", "怎么学英语", "帮我查下快递单号", "写个会议纪要模板", "股票新手怎么入门",
                    "帮我订个会议室", "今天几号", "美元汇率是多少", "怎么注销银行卡", "帮我写年终总结", "跑步减肥有用吗",
                    "世界最高峰是什么", "猫和老鼠好看吗", "帮我改一下错别字", "给我推荐个手机",
                    "这首歌叫什么名字", "帮我算个数学题", "写个万能检讨书", "怎么办理护照", "蜂王浆有什么功效",
                    "帮我备份微信聊天记录", "打印机卡纸怎么办", "写一封投诉信", "垃圾分类怎么分", "帮我起个英文名",
                    "哪家航空公司服务好", "怎么煮溏心蛋", "写个婚礼致辞", "附近的加油站", "帮我改简历", "怎么注册公司"]
            for c in nons:
                if len([r for r in rows if r["label"] == label]) >= n:
                    break
                if (c, label) not in existed and c not in TEST_INTENT_TEXTS:
                    existed.add((c, label))
                    rows.append({"text": c, "label": label})
        else:
            rows.extend(sample_until(tpls[label], n, label, existed))
    return rows


def gen_followup():
    # SNAPSHOT：结果集上直接能做的操作（排序/限条/换图表/读数）
    snap_tpls = [
        "取排名前{N}的", "只看前{N}条", "按{M}从高到低排", "换成柱状图", "用饼图展示", "倒序排列",
        "取前{N}名", "只显示前{N}个", "按{M}升序排", "把表格换成折线图", "取排名倒数前{N}",
        "从高到低排一下", "换成面积图", "只要前{N}行", "第一名是多少？", "哪个月最高？比最低月高多少？",
    ]
    # NEW_QUERY：需要发新查询（换条件/换指标/换维度/换粒度）
    new_tpls = [
        "那{P2}呢？", "再看看{M2}", "换成{Y}年的数据", "看下{Y}年的情况", "按{E2}维度拆一下",
        "只看{R}的", "{R}的情况呢", "把{M2}也加上", "改成统计{M2}", "换成按月汇总",
        "换成二级医院再统计一次。", "把上面的结果按月份拆分，重新看医院。",
        "还有其他渠道业态的订单么？金额又是多少？", "只看{B}业态的", "换成按季度重新汇总",
        "{Y}年同期的数据呢", "换成三级医院再看一下",
    ]
    seen, out = set(), []
    quota = {"SNAPSHOT": 100, "NEW_QUERY": 100}
    for _ in range(6000):
        if all(sum(1 for r in out if r["label"] == k) >= v for k, v in quota.items()):
            break
        base = random.choice(BASE_QS)
        lab = "SNAPSHOT" if sum(1 for r in out if r["label"] == "SNAPSHOT") < quota["SNAPSHOT"] else "NEW_QUERY"
        if lab == "SNAPSHOT":
            text = random.choice(snap_tpls).format(N=random.choice(NUMS), M=random.choice(METRIC_COLLO))
        else:
            text = random.choice(new_tpls).format(
                P2=random.choice(PRODUCTS), M2=random.choice(METRICS + METRIC_COLLO),
                Y=random.choice(["2024", "2023"]), E2=random.choice(ENTITIES),
                R=random.choice(PROVINCES), B=random.choice(BIZ_TYPES))
        # 测试集追问的原句和配对都不进训练集
        if text in TEST_FOLLOWUP_TEXTS or (base, text) in TEST_FOLLOWUP_PAIRS:
            continue
        if (base, text) not in seen and text not in TEST_INTENT_TEXTS:
            seen.add((base, text))
            out.append({"context": base, "text": text, "label": lab})
    return out


def load_real_questions():
    """从实际业务问题.md 按文件顺序读出 (会话, 问题) 序列。"""
    text = (HERE.parent / "实际业务问题.md").read_text(encoding="utf-8")
    recs = re.findall(r'- 会话："([^"]+)"\n- 消息："[^"]*"\n- 问题：\n\n> (.+)', text)
    return recs


# 真实问句打标规则：按顺序匹配，命中即停；只收高置信度的模式
INTENT_RULES = [
    ("闲聊", re.compile(r'^(你好|您好|hi|hello|在吗|在不在|谢谢|多谢|辛苦|再见|拜拜|晚安|早上好|下午好|晚上好|早啊|好的?|收到|明白|懂了|ok|嗯+|哈哈|嘿嘿)[!！。.~\s]*$', re.I)),
    ("解释口径", re.compile(r'(怎么定义|如何定义|是怎么算|怎么算|是什么意思|是什么含义|计算公式|的公式|口径是什么|口径包含|统计口径是|定义是|怎么统计的|如何计算|什么叫|是什么口径)')),
    ("预测分析", re.compile(r'(预测|预期|预计|预估|能完成.{0,8}目标|完成全年目标|未来.{0,6}(走势|趋势|预期))')),
    ("归因分析", re.compile(r'(为什么|啥原因|什么原因|原因是什么|归因|主要靠|因素|驱动|导致)')),
    ("异常分析", re.compile(r'(异常|波动|骤降|暴跌|暴涨|异常点|明显偏低|明显偏高)')),
    ("对比分析", re.compile(r'(对比|相比|比较|哪个高|哪个多|哪个低|差距|同比|环比|差别|差异)')),
    ("趋势分析", re.compile(r'(趋势|走势|变化情况|涨还是跌|按月看|^按(月|季度|年|周)(统计|看|分析)|近\d+个月.{0,8}(变化|走势))')),
    ("占比分析", re.compile(r'(占比|比例|贡献|构成|分布|份额)')),
    ("报表生成", re.compile(r'(月报|周报|日报|报表|报告|汇报材料)')),
    ("明细查询", re.compile(r'(名单|明细|清单|列出|列表|使用科室|供到哪些|有哪些|合作了哪些|销售了哪些|哪些产品|哪些经销商|哪些医院|展示.{0,10}(名称|编码))')),
    ("指标查询", re.compile(r'(是多少|多少钱|多少|统计|查询|查一下|排名前|top\s*\d+|一共|有几个|几笔|最高|最低|最大|最多|最少|是谁|销量|销售额|销售总额|订单笔数|覆盖率|合作时长)', re.I)),
]

# 追问判定规则：SNAPSHOT 是结果集上直接能做的操作，NEW_QUERY 需要发新查询
SNAPSHOT_RULE = re.compile(r'(排名前|取前|只要前|只看前|只显示前|前\s*\d+\s*(名|条|个|行)|从高到低|从低到高|倒序|降序|升序|排序|排一下|柱状图|饼图|折线图|面积图|表格换成|换成.{0,4}图|第一名|哪个月最高|哪个最高|最高的.{0,6}是多少|最低的.{0,6}是多少)')
NEW_QUERY_RULE = re.compile(r'(换成|换一下|换一版|再看|再查|再统计|再分析|那.{1,20}呢|还有其他|加上|加个|加一|只看.{1,8}的|按.{1,6}(拆|维度|重新|分组|汇总|统计)|重新|改成|改看|按月|按季度|按年|同期|同期的|多看|拆分)')
# 话题切换/放弃前问句的追问不算快照操作
TOPIC_SWITCH_RULE = re.compile(r'(换个话题|切换话题|回到|算了|不查了|不看了)')

# 用户点名要收录的真实问句，绕过限量强制入训练集
PINNED_INTENT = [
    {"text": "2025年复旦大学附属华山医院销售订单金额最多的销售员是谁？销售了哪些产品？", "label": "指标查询"},
    {"text": "2025 年上海市，平均每单销售金额是多少？", "label": "指标查询"},
    {"text": "按季度统计2025年销售总金额，按月份统计2025年订单笔数", "label": "趋势分析"},
    {"text": "查询上海地区各经销商的区域医院覆盖率", "label": "指标查询"},
    {"text": "统计各经销商的含税销售总额；按从高到低排序看看；", "label": "指标查询"},
]
PINNED_FOLLOWUP = [
    {"context": "统计各经销商的含税销售总额", "text": "按从高到低排序看看", "label": "SNAPSHOT"},
]


def label_real_question(q):
    """按子句切分后取第一个能命中规则的子句的标签，处理复合问句。"""
    segs = [s.strip() for s in re.split(r'[？?；;\n。]', q) if s.strip()] or [q]
    for seg in segs:
        for label, pat in INTENT_RULES:
            if pat.search(seg):
                return label
    # 子句全不命中再退回整句匹配
    for label, pat in INTENT_RULES:
        if pat.search(q):
            return label
    return None


def mine_real_intent(existed, caps):
    """真实原句按规则打标，每类限量。"""
    recs = load_real_questions()
    out, got = [], Counter()
    seen_texts = set()
    for _, q in recs:
        q = q.strip()
        if not q or q in seen_texts or q in TEST_INTENT_TEXTS:
            continue
        label = label_real_question(q)
        if label is None or got[label] >= caps.get(label, 0):
            continue
        if (q, label) not in existed:
            existed.add((q, label))
            seen_texts.add(q)
            out.append({"text": q, "label": label, "src": "real"})
            got[label] += 1
    return out, got


def mine_real_followup():
    """同一会话内相邻两句构成 (上文, 本轮) 对，短追问按规则打 SNAPSHOT/NEW_QUERY。"""
    recs = load_real_questions()
    by_sess = {}
    for sess, q in recs:
        by_sess.setdefault(sess, []).append(q.strip())
    pairs = []
    for sess, qs in by_sess.items():
        for prev, cur in zip(qs, qs[1:]):
            if not cur or len(cur) > 40 or cur == prev:
                continue
            # 话题切换/放弃类话术不做追问样本
            if TOPIC_SWITCH_RULE.search(cur):
                continue
            if SNAPSHOT_RULE.search(cur) and not re.match(r'^(查询|统计|查一下|帮我查)', cur):
                # 以查询/统计开头的完整指令即使带排名字样也是新查询
                pairs.append({"context": prev, "text": cur, "label": "SNAPSHOT", "src": "real"})
            elif SNAPSHOT_RULE.search(cur) and len(cur) <= 12:
                pairs.append({"context": prev, "text": cur, "label": "SNAPSHOT", "src": "real"})
            elif NEW_QUERY_RULE.search(cur):
                pairs.append({"context": prev, "text": cur, "label": "NEW_QUERY", "src": "real"})
    # 去重并限量；测试集追问的原句和配对都排除
    seen, out = set(), {"SNAPSHOT": [], "NEW_QUERY": []}
    for p in pairs:
        key = (p["context"], p["text"])
        if key in seen or p["text"] in TEST_FOLLOWUP_TEXTS or key in TEST_FOLLOWUP_PAIRS:
            continue
        seen.add(key)
        if len(out[p["label"]]) < 100:
            out[p["label"]].append(p)
    return out["SNAPSHOT"] + out["NEW_QUERY"]


def main():
    out_path = HERE / "train_draft.json"
    existed = set((x["text"], x["label"]) for x in [])
    synthetic_intent = gen_intent()
    for x in synthetic_intent:
        existed.add((x["text"], x["label"]))
    # 真实原句打标混入，每类限量
    caps = {"指标查询": 150, "明细查询": 100, "趋势分析": 60, "对比分析": 60, "占比分析": 60,
            "异常分析": 30, "归因分析": 40, "预测分析": 30, "报表生成": 20, "解释口径": 40,
            "闲聊": 20, "非数据任务": 0}
    real_intent, got = mine_real_intent(existed, caps)
    # 用户点名的问句强制收录
    pinned = []
    for x in PINNED_INTENT:
        if (x["text"], x["label"]) not in existed and x["text"] not in TEST_INTENT_TEXTS:
            existed.add((x["text"], x["label"]))
            pinned.append({**x, "src": "pinned"})
    data = {"intent": synthetic_intent + pinned + real_intent,
            "followup": PINNED_FOLLOWUP + gen_followup() + mine_real_followup()}
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print("intent:", len(data["intent"]), "(synthetic %d + pinned %d + real %d)"
          % (len(synthetic_intent), len(pinned), len(real_intent)))
    for lab, c in Counter(x["label"] for x in data["intent"]).most_common():
        print("  %s: %d" % (lab, c))
    print("real intent hits:", dict(got))
    print("followup:", len(data["followup"]), Counter(x["label"] for x in data["followup"]),
          "real:", sum(1 for x in data["followup"] if x.get("src") == "real"))
    print("saved ->", out_path)


if __name__ == "__main__":
    main()
