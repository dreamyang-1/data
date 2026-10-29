# laya 微调：冻结 mmBERT 编码器，只训决策头（意图分类 + 追问判定）
# 编码器输出对固定输入是确定的，先缓存再训头，CPU 上几分钟跑完
# 用法: /root/venvs/laya-poc/bin/python finetune_laya.py
import glob
import json
import os
import random
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path

import torch
import torch.nn.functional as F
from safetensors.torch import load_file, save_file

HERE = Path(__file__).parent
_snapshots = glob.glob(os.path.expanduser(
    "~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/*/multilingual"))
if not _snapshots:
    raise FileNotFoundError("本地缓存里没有 laya multilingual checkpoint，请先跑一次 laya 评测或手动下载")
# 按修改时间取最新，避免字符串排序对版本号失真
SNAPSHOT = max(_snapshots, key=lambda p: os.path.getmtime(p))
OUT_DIR = HERE / "finetuned"
SEED = 20261014
LR = 1e-4
EPOCHS = 12
BATCH = 32
PATIENCE = 4
VAL_RATIO = 0.15

random.seed(SEED)
torch.manual_seed(SEED)
# 限制线程并降低调度优先级，避免影响机器上的其他服务
torch.set_num_threads(min(32, (os.cpu_count() or 8) // 4))
torch.set_num_interop_threads(2)
try:
    os.nice(5)
except Exception:
    pass

# 题目定义：训练和评测用同一份，保证格式一致
INTENT_LABELS = ["指标查询", "明细查询", "趋势分析", "对比分析", "占比分析", "异常分析",
                 "归因分析", "预测分析", "报表生成", "解释口径", "闲聊", "非数据任务"]
INTENT_Q = {"t": "choice", "ins": "判断用户提问属于哪类数据业务意图",
            "crit": {lab: None for lab in INTENT_LABELS}}
FOLLOWUP_Q = {"t": "choice", "ins": "根据对话上文判断本轮追问应复用上一轮查询结果还是发起新查询",
              "crit": {"SNAPSHOT": None, "NEW_QUERY": None}}


def load_model(model_dir):
    """按 laya Agent 的加载方式构建模型并载入权重。"""
    from transformers import AutoTokenizer
    from laya.common import build_model
    with open(os.path.join(model_dir, "rl_agent_config.json")) as f:
        cfg = json.load(f)
    tok = AutoTokenizer.from_pretrained(os.path.join(model_dir, "tokenizer"))
    model = build_model(cfg, encoder_dir=os.path.join(model_dir, "encoder")
                        if os.path.exists(os.path.join(model_dir, "encoder")) else None)
    weights = load_file(os.path.join(model_dir, "model.safetensors"))
    model.load_state_dict(weights, strict=True)
    try:
        model.encoder.config.reference_compile = False
    except Exception:
        pass
    model.eval()
    return cfg, tok, model


def build_item(tok, state, q, label_idx, max_len=1024, head_max_len=256):
    """state 就是送进序列的用户侧内容；意图题为问题原文，追问为上文+本轮。"""
    from laya.common import QTYPES, build_sequence, render_options
    seq, markers = build_sequence(tok, state, q, max_len, head_max_len)
    assert len(markers) == len(render_options(q)), "选项超出 head_max_len"
    return {"ids": seq, "markers": markers, "qtype": QTYPES[q["t"]], "label_idx": label_idx}


def make_state(row, kind):
    if kind == "intent":
        return row["text"]
    return "上文：%s\n本轮：%s" % (row.get("context", ""), row["text"])


def make_items(tok, rows, kind):
    """rows: intent 传 {text,label}，followup 传 {context,text,label}。返回 (items, dropped)。"""
    q = INTENT_Q if kind == "intent" else FOLLOWUP_Q
    opts = list(q["crit"].keys())
    items, dropped = [], 0
    for r in rows:
        lab = r["label"]
        if lab not in opts:
            dropped += 1
            continue
        if kind == "followup" and not r.get("context"):
            # 没有上文的追问无从判断，直接丢弃
            dropped += 1
            continue
        it = build_item(tok, make_state(r, kind), q, opts.index(lab))
        it["task"] = kind
        it["text"] = r["text"]
        items.append(it)
    return items, dropped


def drop_test_overlap(items, test_path):
    """防止训练样本与测试集文本撞车，撞上的剔除并告警。"""
    test = json.loads(Path(test_path).read_text(encoding="utf-8"))
    banned = set(x["text"] for x in test["intent"]) | set(x["text"] for x in test["followup"])
    kept = [it for it in items if it["text"] not in banned]
    if len(kept) < len(items):
        print("[warn] 剔除与测试集文本重复的训练样本 %d 条" % (len(items) - len(kept)))
    return kept


def split_val(rows):
    """按标签分层抽 val，返回 (train_rows, val_rows)。"""
    by_lab = defaultdict(list)
    for r in rows:
        by_lab[r["label"]].append(r)
    train, val = [], []
    for rs in by_lab.values():
        rs = rs[:]
        random.shuffle(rs)
        n_val = max(1, int(len(rs) * VAL_RATIO))
        val.extend(rs[:n_val])
        train.extend(rs[n_val:])
    random.shuffle(train)
    return train, val


@torch.no_grad()
def cache_hidden(model, items, pad_id, batch=16):
    """编码器冻结，输出确定：一次性算好隐藏状态存内存，训练时只前向决策头。"""
    from laya.common import collate_items
    order = sorted(range(len(items)), key=lambda i: len(items[i]["ids"]))
    for s in range(0, len(order), batch):
        chunk = [items[i] for i in order[s:s + batch]]
        b = collate_items([chunk], pad_id)
        with torch.no_grad():
            h = model.encoder(input_ids=b["input_ids"], attention_mask=b["attention_mask"]).last_hidden_state
        h = h.to(torch.float16)
        for j, it in enumerate(chunk):
            # 按各自真实长度裁剪，训练 batch 里再统一 pad
            n = len(it["ids"])
            it["h"] = h[j][:n]
            it["att"] = b["attention_mask"][j][:n].bool()


def pad_chunk(chunk):
    """把缓存的不定长隐藏状态 pad 成同长度张量。"""
    L = max(it["h"].shape[0] for it in chunk)
    d = chunk[0]["h"].shape[1]
    hs, atts = [], []
    for it in chunk:
        n = it["h"].shape[0]
        h = it["h"].new_zeros((L, d))
        h[:n] = it["h"]
        a = torch.zeros(L, dtype=torch.bool)
        a[:n] = it["att"]
        hs.append(h)
        atts.append(a)
    return torch.stack(hs), torch.stack(atts)


def head_forward(model, h, att, mpos, mmask, qtype):
    """复现 DecisionModel.forward 的决策头部分（编码器输出已缓存）。"""
    h = h.float()
    h = h + model.type_emb(qtype)[:, None, :]
    pad = ~att
    for layer in model.head.layers:
        h = layer(h, src_key_padding_mask=pad)
    idx = mpos.clamp(min=0)[:, :, None].expand(-1, -1, h.size(-1))
    m = torch.gather(h, 1, idx)
    logits = model.scorer(m).squeeze(-1).float()
    logits = logits.masked_fill(~mmask, -1e4)
    return logits


def train_head(model, train_items, val_items, pad_id):
    head_params = [p for n, p in model.named_parameters() if not n.startswith("encoder.")]
    enc_snapshot = {k: v.clone() for k, v in model.state_dict().items() if k.startswith("encoder.")}
    opt = torch.optim.AdamW(head_params, lr=LR, weight_decay=0.01)
    model.head.train()
    model.scorer.train()
    best_acc, best_state, bad = -1.0, None, 0
    for ep in range(1, EPOCHS + 1):
        random.shuffle(train_items)
        t0 = time.time()
        total, correct, loss_sum = 0, 0, 0.0
        for s in range(0, len(train_items), BATCH):
            chunk = train_items[s:s + BATCH]
            from laya.common import collate_items
            b = collate_items([chunk], pad_id)
            h, att = pad_chunk(chunk)
            logits = head_forward(model, h, att, b["marker_pos"], b["marker_mask"], b["qtype"])
            y = torch.tensor([it["label_idx"] for it in chunk])
            loss = F.cross_entropy(logits, y)
            opt.zero_grad()
            loss.backward()
            opt.step()
            loss_sum += loss.item() * len(chunk)
            correct += (logits.argmax(-1) == y).sum().item()
            total += len(chunk)
        vacc, vlogits, vy, vtasks = eval_cached(model, val_items, pad_id)
        # 两个任务分开看，联合训练若互相干扰这里能直接暴露
        per_task = {}
        for task in ("intent", "followup"):
            idx = [i for i, t in enumerate(vtasks) if t == task]
            if idx:
                per_task[task] = (vlogits.argmax(-1)[idx] == vy[idx]).float().mean().item()
        mark = ""
        if vacc > best_acc:
            best_acc, bad = vacc, 0
            best_state = {k: v.clone() for k, v in model.state_dict().items() if not k.startswith("encoder.")}
            mark = "  <- best"
        else:
            bad += 1
        print("epoch %2d  train_acc %.3f  loss %.4f  val_acc %.3f (intent %.3f / followup %.3f)  %.0fs%s"
              % (ep, correct / total, loss_sum / total, vacc,
                 per_task.get("intent", 0), per_task.get("followup", 0), time.time() - t0, mark), flush=True)
        if bad >= PATIENCE:
            print("val 连续 %d 轮无提升，提前停止" % PATIENCE)
            break
    if best_state is None:
        raise RuntimeError("验证集一次都没超过初始准确率，训练异常，请检查数据")
    # 回滚到最优头权重；编码器冻结本就不动，这里一并保证
    model.load_state_dict({**enc_snapshot, **best_state}, strict=True)
    model.eval()
    return best_acc


@torch.no_grad()
def eval_cached(model, items, pad_id):
    """缓存特征上的准确率和 logits（温度拟合用）。"""
    model.head.eval()
    model.scorer.eval()
    all_logits, ys, tasks = [], [], []
    for s in range(0, len(items), BATCH):
        chunk = items[s:s + BATCH]
        from laya.common import collate_items
        b = collate_items([chunk], pad_id)
        h, att = pad_chunk(chunk)
        logits = head_forward(model, h, att, b["marker_pos"], b["marker_mask"], b["qtype"])
        all_logits.append(logits)
        ys.extend(it["label_idx"] for it in chunk)
        tasks.extend(it["task"] for it in chunk)
    logits = torch.cat(all_logits)
    y = torch.tensor(ys)
    acc = (logits.argmax(-1) == y).float().mean().item()
    model.head.train()
    model.scorer.train()
    return acc, logits, y, tasks


def fit_temperature(logits, y):
    """在验证集上按 NLL 网格搜索温度，范围与 laya 的 clamp 一致。"""
    best_t, best_nll = 1.0, None
    for t in [round(0.5 + 0.05 * i, 2) for i in range(91)]:
        nll = F.cross_entropy(logits / t, y).item()
        if best_nll is None or nll < best_nll:
            best_t, best_nll = t, nll
    return best_t


@torch.no_grad()
def eval_testset(model, tok, test_path):
    """在 120 条测试集上评测，返回 (意图准确率, 追问准确率)。"""
    from laya.common import collate_items
    test = json.loads(Path(test_path).read_text(encoding="utf-8"))

    def run(rows, q, kind):
        opts = list(q["crit"].keys())
        items, hits = [], 0
        for r in rows:
            lab = r["label"]
            items.append(build_item(tok, make_state(r, kind), q, opts.index(lab)))
        for s in range(0, len(items), BATCH):
            chunk = items[s:s + BATCH]
            b = collate_items([chunk], tok.pad_token_id)
            logits, _ = model(b["input_ids"], b["attention_mask"], b["marker_pos"],
                              b["marker_mask"], b["qtype"])
            y = torch.tensor([it["label_idx"] for it in chunk])
            hits += (logits.argmax(-1) == y).sum().item()
        return hits, len(items)

    ih, itotal = run(test["intent"], INTENT_Q, "intent")
    fh, ftotal = run(test["followup"], FOLLOWUP_Q, "followup")
    return ih / itotal, fh / ftotal, ih, itotal, fh, ftotal


def save_model(model, cfg, temperature, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    sd = {k: v.contiguous().clone() for k, v in model.state_dict().items()}
    save_file(sd, str(out_dir / "model.safetensors"))
    cfg = dict(cfg)
    cfg["temperature"] = [temperature, cfg.get("temperature", [1, 1, 1])[1], cfg.get("temperature", [1, 1, 1])[2]]
    with open(out_dir / "rl_agent_config.json", "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    shutil.copytree(os.path.join(SNAPSHOT, "tokenizer"), out_dir / "tokenizer", dirs_exist_ok=True)
    shutil.copytree(os.path.join(SNAPSHOT, "encoder"), out_dir / "encoder", dirs_exist_ok=True)
    print("已保存 ->", out_dir, flush=True)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--separate", action="store_true",
                    help="对照组：意图和追问各训一个独立头，保存到 finetuned_intent/ finetuned_followup/")
    args = ap.parse_args()

    print("checkpoint:", SNAPSHOT)
    cfg, tok, model = load_model(SNAPSHOT)
    pad_id = tok.pad_token_id

    ih0, fh0, ih0n, it0, fh0n, ft0 = eval_testset(model, tok, HERE / "laya_poc_dataset.json")
    print("微调前: 意图 %d/%d = %.3f  追问 %d/%d = %.3f" % (ih0n, it0, ih0, fh0n, ft0, fh0), flush=True)

    draft = json.loads((HERE / "train_draft.json").read_text(encoding="utf-8"))
    train_rows, val_rows = split_val(draft["intent"])
    ftrain_rows, fval_rows = split_val(draft["followup"])
    train_items, d1 = make_items(tok, train_rows, "intent")
    val_items, d2 = make_items(tok, val_rows, "intent")
    ftrain_items, d3 = make_items(tok, ftrain_rows, "followup")
    fval_items, d4 = make_items(tok, fval_rows, "followup")
    # 兜底：任何与测试集文本撞车的样本一律剔除（生成端已过滤，这里再核一遍）
    train_items = drop_test_overlap(train_items, HERE / "laya_poc_dataset.json")
    ftrain_items = drop_test_overlap(ftrain_items, HERE / "laya_poc_dataset.json")
    val_items = drop_test_overlap(val_items, HERE / "laya_poc_dataset.json")
    fval_items = drop_test_overlap(fval_items, HERE / "laya_poc_dataset.json")
    print("train %d (drop %d)  val %d (drop %d)  ftrain %d (drop %d)  fval %d (drop %d)"
          % (len(train_items), d1, len(val_items), d2, len(ftrain_items), d3, len(fval_items), d4))

    all_items = train_items + val_items + ftrain_items + fval_items
    print("缓存编码器输出...", flush=True)
    t0 = time.time()
    cache_hidden(model, all_items, pad_id)
    print("缓存完成 %.0fs" % (time.time() - t0), flush=True)

    # 编码器冻结且输出已缓存，联合/独立两种训练共用这份缓存
    results = {}
    if args.separate:
        # 对照组 A：意图独立训一个头
        print("\n----- 独立训练：意图 -----", flush=True)
        train_head(model, train_items, val_items, pad_id)
        _, vlogits, vy, _ = eval_cached(model, val_items, pad_id)
        save_model(model, cfg, fit_temperature(vlogits, vy), HERE / "finetuned_intent")
        results["intent独立"] = eval_testset(model, tok, HERE / "laya_poc_dataset.json")

        # 对照组 B：追问独立训一个头，头权重重置回原始状态
        print("\n----- 独立训练：追问 -----", flush=True)
        cfg2, _, model = load_model(SNAPSHOT)
        cache_hidden(model, all_items, pad_id)
        train_head(model, ftrain_items, fval_items, pad_id)
        _, vlogits, vy, _ = eval_cached(model, fval_items, pad_id)
        save_model(model, cfg2, fit_temperature(vlogits, vy), HERE / "finetuned_followup")
        results["followup独立"] = eval_testset(model, tok, HERE / "laya_poc_dataset.json")
        print("\n独立头: 意图 %.3f  追问 %.3f" % (results["intent独立"][0], results["followup独立"][1]))

    # 主实验：两任务联合训一个头
    print("\n----- 联合训练 -----", flush=True)
    cfg, _, model = load_model(SNAPSHOT)
    train_head(model, train_items + ftrain_items, val_items + fval_items, pad_id)

    # 温度拟合复用验证集，结果偏乐观，只作参考值（准确率不受温度影响）
    vacc, vlogits, vy, _ = eval_cached(model, val_items + fval_items, pad_id)
    temperature = fit_temperature(vlogits, vy)
    print("val_acc %.3f  fitted temperature %.2f" % (vacc, temperature))

    save_model(model, cfg, temperature, OUT_DIR)

    # 用保存目录重新加载做端到端验证
    _, _, model2 = load_model(str(OUT_DIR))
    ih1, fh1, ih1n, it1, fh1n, ft1 = eval_testset(model2, tok, HERE / "laya_poc_dataset.json")
    print("\n===== 结果对比（120 条测试集，仅代表这批样本，泛化能力以验证集和线上实测为准）=====")
    print("联合头   意图: %.3f -> %.3f  (%d/%d)" % (ih0, ih1, ih1n, it1))
    print("联合头   追问: %.3f -> %.3f  (%d/%d)" % (fh0, fh1, fh1n, ft1))
    if args.separate:
        print("独立头   意图: %.3f  追问: %.3f  （对照 --separate）"
              % (results["intent独立"][0], results["followup独立"][1]))


if __name__ == "__main__":
    main()
