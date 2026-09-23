"""Дообучение Laya на выгрузке `laya_dataset.py`. Один процесс, одна GPU.

    python training/laya_finetune.py --data data/train/laya --out models/laya-fuckhr

Цикл взят из официального ноутбука Laya
(`notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb`, Apache-2.0):
RLCD — градиент политики с правильным правилом оценки плюс мягкая
кросс-энтропия, затем температуры на отложенном срезе. Отличия от ноутбука:
нет DDP (ноутбук требует ровно две T4 и падает на одной), пути и чекпойнт
приходят аргументами, в конце — замер на `test.jsonl` с проверкой порядка
вариантов. Инструкция — docs/laya-finetune.md.

Не часть пайплайна: запускается там, где есть GPU (Kaggle, Colab, WSL).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from pathlib import Path

import torch
from huggingface_hub import snapshot_download
from safetensors.torch import load_file, save_file
from transformers import AutoTokenizer

from laya.agent import _fix_tokenizer_config
from laya.common import QTYPES, build_model, build_sequence, proper_reward, render_options

SEED = 20260922


def model_dir_of(base: str) -> str:
    if os.path.isdir(base):
        return base
    names = ("rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")
    return snapshot_download(base, allow_patterns=list(names))


def items_of(path: Path, tok, cfg) -> list[dict]:
    """Строки typed-decisions → токенизированные вопросы, как в ячейке 6 ноутбука."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        state, questions, gold = (json.loads(row[k]) for k in ("state", "questions", "gold"))
        for qid, q in questions.items():
            crit = q["criteria"]
            target = [float(gold[qid]["probabilities"].get(k, 0.0)) for k in crit]
            total = sum(target) or 1.0
            target = [v / total for v in target]
            internal = {"t": q["type"], "ins": q["instructions"], "crit": crit}
            seq, markers = build_sequence(tok, state, internal, cfg["max_len"], cfg["head_max_len"])
            if len(markers) != len(render_options(internal)):
                continue
            out.append({"ids": seq, "markers": markers, "qtype": QTYPES[q["type"]],
                        "target": target, "label": target.index(max(target))})
    return out


def collate(items, pad_id):
    n, width = len(items), max(len(it["ids"]) for it in items)
    kmax = max(len(it["markers"]) for it in items)
    ids = torch.full((n, width), pad_id, dtype=torch.long)
    att = torch.zeros((n, width), dtype=torch.long)
    mpos = torch.zeros((n, kmax), dtype=torch.long)
    mmask = torch.zeros((n, kmax), dtype=torch.bool)
    target = torch.zeros((n, kmax))
    for i, it in enumerate(items):
        ids[i, : len(it["ids"])] = torch.tensor(it["ids"])
        att[i, : len(it["ids"])] = 1
        k = len(it["markers"])
        mpos[i, :k] = torch.tensor(it["markers"])
        mmask[i, :k] = True
        target[i, :k] = torch.tensor(it["target"])
    return ids, att, mpos, mmask, target, torch.tensor([it["qtype"] for it in items])


def fit_temperature(pairs) -> float:
    """Одна температура на тип вопроса по отложенному срезу (ячейка 8 ноутбука)."""
    if len(pairs) < 10:
        return 1.0
    kmax = max(len(z) for z, _ in pairs)
    logits = torch.full((len(pairs), kmax), -1e4)
    target = torch.zeros((len(pairs), kmax))
    for i, (z, t) in enumerate(pairs):
        logits[i, : len(z)] = torch.tensor(z)
        target[i, : len(t)] = torch.tensor(t)
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = -(target * torch.log_softmax(logits / log_t.exp(), -1)).sum(-1).mean()
        loss.backward()
        return loss

    opt.step(closure)
    return float(torch.clamp(log_t.exp(), 0.5, 5.0).item())


def train(args) -> Path:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    cuda = device.type == "cuda"
    model_dir = model_dir_of(args.base)
    _fix_tokenizer_config(model_dir)
    tok = AutoTokenizer.from_pretrained(os.path.join(model_dir, "tokenizer"))
    cfg = json.loads(Path(model_dir, "rl_agent_config.json").read_text())

    items = items_of(Path(args.data) / "train.jsonl", tok, cfg)
    if not items:
        raise SystemExit("train.jsonl пуст: сначала python laya_dataset.py")
    order = list(range(len(items)))
    random.Random(SEED).shuffle(order)
    n_calib = min(400, len(items) // 10)
    calib = [items[i] for i in order[:n_calib]]
    train_items = [items[i] for i in order[n_calib:]]

    try:
        from transformers.initialization import no_init_weights
    except ImportError:  # Transformers 4.x
        from transformers.modeling_utils import no_init_weights
    with no_init_weights():  # иначе случайные веса создаются зря и удваивают пик памяти
        model = build_model(cfg, encoder_dir=os.path.join(model_dir, "encoder"))
    model.load_state_dict(load_file(os.path.join(model_dir, "model.safetensors")), strict=True)
    if cuda:
        model.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.head_checkpointing = True
    model.to(device).train()

    enc = [p for n, p in model.named_parameters() if "encoder." in n]
    head = [p for n, p in model.named_parameters() if "encoder." not in n]
    groups = [{"params": head, "lr": args.lr_head}]
    if args.freeze_encoder:
        # Только голова: влезает в 4–8 ГБ без GPU, но и учится заметно слабее.
        for param in enc:
            param.requires_grad_(False)
    else:
        groups.append({"params": enc, "lr": args.lr_encoder})
    opt = torch.optim.AdamW(groups, weight_decay=0.01)
    steps = max(1, len(train_items) // (args.batch * args.accum)) * args.epochs
    if args.max_steps:
        steps = min(steps, args.max_steps)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=steps, eta_min=1e-6)
    scaler = torch.amp.GradScaler("cuda", enabled=cuda)
    print("обучающих вопросов {}, на калибровку {}, шагов {}, {}".format(
        len(train_items), len(calib), steps, device))

    done, started = 0, time.time()
    for epoch in range(args.epochs):
        random.Random(SEED + epoch).shuffle(train_items)
        sigma = 0.4 + (0.1 - 0.4) * (epoch / max(1, args.epochs - 1))
        for n, start in enumerate(range(0, len(train_items), args.batch), start=1):
            ids, att, mpos, mmask, target, qtype = (
                t.to(device) for t in collate(train_items[start:start + args.batch], tok.pad_token_id))
            with torch.autocast(device.type, dtype=torch.float16, enabled=cuda):
                logits, act = model(ids, att, mpos, mmask, qtype)
            logits = logits.float()
            k = mmask.sum(-1, keepdim=True).float()
            eps = torch.randn((4,) + logits.shape, device=device) * sigma * mmask
            eps = (eps - eps.sum(-1, keepdim=True) / k) * mmask
            z = logits.detach().unsqueeze(0) + eps
            q = torch.softmax(z.masked_fill(~mmask, -1e4), -1)
            with torch.no_grad():
                r = proper_reward(q, target.unsqueeze(0), qtype, mmask, w_sph=0.75, w_rps=1.0)
                adv = (r - r.mean(0, keepdim=True)) / (r.std() + 1e-6)
            logp = -(((z - logits.unsqueeze(0)) ** 2) * mmask).sum(-1) / (2 * sigma ** 2)
            ce = -(target * torch.log_softmax(logits.masked_fill(~mmask, -1e4), -1)).sum(-1).mean()
            loss = (-(adv * logp).mean() + ce) / args.accum + 0.0 * act.sum()
            scaler.scale(loss).backward()
            if n % args.accum == 0 or start + args.batch >= len(train_items):
                scaler.unscale_(opt)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                scaler.step(opt)
                scaler.update()
                sched.step()
                opt.zero_grad(set_to_none=True)
                done += 1
                if done % 20 == 0:
                    print("  эпоха {} шаг {}/{} loss {:.4f} ({:.0f} с)".format(
                        epoch + 1, done, steps, loss.item() * args.accum, time.time() - started))
                if done >= steps:
                    break
        if done >= steps:
            break

    model.eval()
    pairs: dict[int, list] = {0: [], 1: [], 2: []}
    with torch.no_grad():
        for start in range(0, len(calib), 16):
            chunk = calib[start:start + 16]
            ids, att, mpos, mmask, _, qtype = (t.to(device) for t in collate(chunk, tok.pad_token_id))
            logits, _ = model(ids, att, mpos, mmask, qtype)
            for row, it in enumerate(chunk):
                pairs[it["qtype"]].append((logits[row, : len(it["markers"])].float().cpu().tolist(), it["target"]))
    cfg["temperature"] = [fit_temperature(pairs[t]) for t in range(3)]
    cfg.pop("temperature_by_options", None)
    cfg["fine_tuned"] = True
    cfg["model_name"] = "laya-fuckhr"
    print("температуры (choice, score, noul):", [round(t, 3) for t in cfg["temperature"]])

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    save_file({k: v.half().contiguous().cpu() for k, v in model.state_dict().items()},
              str(out / "model.safetensors"))
    shutil.copytree(os.path.join(model_dir, "encoder"), out / "encoder", dirs_exist_ok=True)
    tok.save_pretrained(str(out / "tokenizer"))
    (out / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return out


def auroc(pos, neg):
    if not pos or not neg:
        return None
    return round(sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg)), 3)


def evaluate(model_path: Path, data: Path) -> dict:
    """Точность, Брайер и AUROC обоих порядков вариантов на test.jsonl."""
    import laya  # noqa: PLC0415

    agent = laya.Agent(str(model_path))
    stats: dict[str, dict[str, list]] = {}
    for line in (data / "test.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        state, questions, gold = (json.loads(row[k]) for k in ("state", "questions", "gold"))
        answers = agent.predict(state, questions)["answers"]
        s = stats.setdefault(row["workflow"], {"hit": [], "brier": [], "straight": [], "swapped": []})
        yes = gold["verdict"]["label"] == "A"
        for qid, yes_key in (("verdict", "A"), ("verdict_swapped", "B")):
            p = answers[qid]["probabilities"][yes_key]
            s["hit"].append(float((p >= 0.5) == yes))
            s["brier"].append((p - float(yes)) ** 2)
            s["straight" if qid == "verdict" else "swapped"].append((yes, p))
    report = {}
    for stage, s in stats.items():
        report[stage] = {
            "вопросов": len(s["hit"]),
            "точность": round(sum(s["hit"]) / len(s["hit"]), 3),
            "брайер": round(sum(s["brier"]) / len(s["brier"]), 3),
            "auroc_прямой": auroc([p for y, p in s["straight"] if y], [p for y, p in s["straight"] if not y]),
            "auroc_перевёрнутый": auroc([p for y, p in s["swapped"] if y], [p for y, p in s["swapped"] if not y]),
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Дообучение Laya на своём датасете")
    parser.add_argument("--data", default="data/train/laya")
    parser.add_argument("--base", default="convaiinnovations/laya-multilingual")
    parser.add_argument("--out", default="models/laya-fuckhr")
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--accum", type=int, default=8)
    parser.add_argument("--lr-encoder", type=float, default=2.5e-5)
    parser.add_argument("--lr-head", type=float, default=1.0e-4)
    parser.add_argument("--freeze-encoder", action="store_true", help="учить только голову: без GPU")
    parser.add_argument("--max-steps", type=int, default=0, help="потолок шагов, для пробного прогона")
    parser.add_argument("--eval-only", action="store_true", help="только замер готовой папки --out")
    args = parser.parse_args()

    out = Path(args.out) if args.eval_only else train(args)
    report = evaluate(out, Path(args.data))
    (out / "eval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
