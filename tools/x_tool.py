#!/usr/bin/env python3
"""X投稿 半自動化ツール（標準ライブラリのみ）

使い方（x-posting/ で実行）:
  python3 tools/x_tool.py validate --brand manabiya-project 週次/2026-W44.md
  python3 tools/x_tool.py export   --brand manabiya-project 週次/2026-W44.md
  python3 tools/x_tool.py channels                                   # BufferのチャンネルIDを確認
  python3 tools/x_tool.py push     --brand manabiya-project 週次/2026-W44.md --dry-run
  python3 tools/x_tool.py push     --brand manabiya-project 週次/2026-W44.md [--schedule]
  python3 tools/x_tool.py report   --brand manabiya-project

安全のための約束:
  - Bufferに送るのは status が「承認」の投稿だけ。validate でエラーがある週は送らない。
  - 既定ではBufferに「下書き」として登録する。--schedule を付けたときだけ予約投稿にする。
  - 送信済みの投稿は「登録済」に書き換え、二重登録しない。
  - Bufferのパスワードは使わない。APIキーは環境変数 BUFFER_API_KEY で渡す。
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = dt.timezone(dt.timedelta(hours=9))
BUFFER_ENDPOINT = "https://api.buffer.com"
STATUSES = ("未承認", "承認", "却下", "登録済")
KINDS = ("情報提供", "note紹介")

# 誇張・虚偽につながりやすい表現（見つかったら警告。根拠があれば本文で示す）
RISKY = ["必ず合格", "絶対", "100%", "１００％", "確実に", "誰でも", "簡単に合格", "最短で合格",
         "合格率", "裏ワザ", "知らないと損", "人生が変わる", "爆速", "圧倒的"]

# Buffer GraphQL（developers.buffer.com の Posts & Scheduling ガイドに基づく）。
# 最初の実行で応答を確認し、スキーマ差異があればここだけ直す。
CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id status dueAt } }
    ... on MutationError { message }
  }
}"""
LIST_CHANNELS = """
query {
  account {
    organizations { id name channels { id name service } }
  }
}"""


# ---------------------------------------------------------------- 週次ファイル

def brand_dir(brand):
    d = ROOT / "brands" / brand
    if not d.is_dir():
        sys.exit(f"ブランドフォルダが見つかりません: {d}")
    return d


def parse_week(path):
    """## で始まる見出しごとに1投稿。- key: value と ```text ブロックを読む"""
    posts, cur, in_text = [], None, False
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if in_text:
            if line.startswith("```"):
                in_text = False
            else:
                cur["text_lines"].append(line)
            continue
        if line.startswith("## "):
            cur = {"heading": line[3:].strip(), "line": no, "text_lines": []}
            posts.append(cur)
        elif cur is not None and line.startswith("```text"):
            in_text = True
        elif cur is not None:
            m = re.match(r"-\s*(\w+):\s*(.*)$", line)
            if m:
                cur[m.group(1)] = m.group(2).strip()
    for p in posts:
        p["text"] = "\n".join(p.pop("text_lines")).strip()
    return posts


def set_fields(path, post_id, **fields):
    """指定IDの投稿の - key: value 行を書き換える（なければ id 行の直後に追加）"""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(rf"-\s*id:\s*{re.escape(post_id)}\s*$", l))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    for k, v in fields.items():
        for i in range(start, end):
            if re.match(rf"-\s*{k}:", lines[i]):
                lines[i] = f"- {k}: {v}"
                break
        else:
            lines.insert(start + 1, f"- {k}: {v}")
            end += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def weighted_len(text):
    """Xの文字数の数え方（概算）: URLは23、CJK等の全角は2、それ以外は1。上限280"""
    text = re.sub(r"https?://\S+", "x" * 23, text)
    n = 0
    for ch in text:
        o = ord(ch)
        n += 1 if (o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037) else 2
    return n


def bigrams(s):
    s = re.sub(r"https?://\S+|[\s\W_]+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / len(A | B) if A and B else 0.0


def load_history(bdir):
    p = bdir / "投稿履歴.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_paid_urls(bdir):
    p = bdir / "有料記事リスト.md"
    return set(re.findall(r"https://note\.com/\S+?/n/\w+", p.read_text(encoding="utf-8"))) if p.exists() else set()


def parse_when(p):
    try:
        return dt.datetime.strptime(p.get("datetime", ""), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        return None


def check(bdir, posts):
    """(errors, warnings) を投稿IDごとに返す"""
    errors, warns = defaultdict(list), defaultdict(list)
    history = load_history(bdir)
    paid = load_paid_urls(bdir)
    now = dt.datetime.now(JST)
    seen_ids = Counter(p.get("id") for p in posts)
    for p in posts:
        pid = p.get("id") or f"(行{p['line']})"
        e, w = errors[pid], warns[pid]
        if not p.get("id"):
            e.append("id がありません")
        elif seen_ids[p["id"]] > 1:
            e.append("id が重複しています")
        if p.get("status") not in STATUSES:
            e.append(f"status は {'/'.join(STATUSES)} のいずれかにしてください")
        if p.get("status") == "却下":
            continue
        if p.get("kind") not in KINDS:
            e.append(f"kind は {'/'.join(KINDS)} のいずれかにしてください")
        when = parse_when(p)
        if not when:
            e.append("datetime は「YYYY-MM-DD HH:MM」（日本時間）で書いてください")
        elif when < now and p.get("status") in ("未承認", "承認"):
            e.append("予定日時が過去になっています")
        text = p["text"]
        if not text:
            e.append("本文が空です")
        if "【要記入" in text or "【要記入" in p.get("link", ""):
            e.append("【要記入】の箇所が残っています")
        n = weighted_len(text)
        if n > 280:
            e.append(f"文字数オーバー（X換算 {n}/280。全角なら約{(n - 280 + 1) // 2}文字削る）")
        for word in RISKY:
            if word in text:
                w.append(f"誇張につながりやすい表現「{word}」：根拠を示せない場合は言い換えてください")
        urls = re.findall(r"https?://\S+", text)
        if p.get("kind") == "note紹介":
            if not urls:
                e.append("note紹介の投稿に記事URLがありません")
            elif paid and not any(u in paid for u in urls):
                w.append("URLが有料記事リスト.md にありません")
        for h in history:
            s = similarity(text, h.get("本文", ""))
            if s >= 0.6:
                e.append(f"過去の投稿とほぼ同じです（類似度 {s:.2f}、{h.get('投稿日時', '')}）")
            elif s >= 0.4:
                w.append(f"過去の投稿と似ています（類似度 {s:.2f}、{h.get('投稿日時', '')}）")
    live = [p for p in posts if p.get("status") != "却下"]
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            s = similarity(a["text"], b["text"])
            if s >= 0.5:
                errors[a.get("id", "?")].append(f"{b.get('id')} と内容がほぼ同じです（類似度 {s:.2f}）")
    kinds = Counter(p.get("kind") for p in posts if p.get("status") != "却下")
    total = sum(kinds.values())
    if total and kinds["note紹介"] / total > 0.4:
        warns["(週全体)"].append(f"note紹介が {kinds['note紹介']}/{total} 本です。宣伝の比率が高すぎないか確認してください")
    return errors, warns


# ---------------------------------------------------------------- コマンド

def cmd_validate(args):
    bdir = brand_dir(args.brand)
    path = bdir / args.week
    posts = parse_week(path)
    errors, warns = check(bdir, posts)
    by_status = Counter(p.get("status") for p in posts)
    print(f"{path.relative_to(ROOT)}: {len(posts)}本（" + "、".join(f"{k} {v}" for k, v in by_status.items()) + "）\n")
    for p in posts:
        pid = p.get("id", "?")
        mark = "NG" if errors.get(pid) else ("注意" if warns.get(pid) else "OK")
        print(f"[{mark}] {pid} {p.get('datetime', '')} {p.get('kind', '')} {p.get('status', '')}  X換算 {weighted_len(p['text'])}/280")
        for m in errors.get(pid, []):
            print(f"    エラー: {m}")
        for m in warns.get(pid, []):
            print(f"    注意:   {m}")
    for m in warns.get("(週全体)", []):
        print(f"\n注意: {m}")
    n_err = sum(len(v) for v in errors.values())
    print(f"\nエラー {n_err}件 / 注意 {sum(len(v) for v in warns.values())}件")
    return n_err


def approved(posts):
    return [p for p in posts if p.get("status") == "承認"]


def cmd_export(args):
    bdir = brand_dir(args.brand)
    path = bdir / args.week
    posts = parse_week(path)
    errors, _ = check(bdir, posts)
    ok = [p for p in approved(posts) if not errors.get(p["id"])]
    if not ok:
        sys.exit("書き出せる投稿がありません（status: 承認 かつ エラーなしの投稿のみ書き出します）")
    out = path.with_name(path.stem + "_Buffer貼り付け用.txt")
    blocks = [f"■ {p['datetime']}（日本時間） {p['id']}\n{p['text']}" for p in sorted(ok, key=lambda p: p["datetime"])]
    out.write_text("\n\n" + ("\n\n" + "-" * 30 + "\n\n").join(blocks) + "\n", encoding="utf-8")
    print(f"{len(ok)}本を書き出しました: {out.relative_to(ROOT)}")
    print("Bufferの画面で、各投稿を表示の日時で予約してください。予約したら status を「登録済」に変えてください。")


def buffer_call(query, variables=None):
    key = os.environ.get("BUFFER_API_KEY")
    if not key:
        sys.exit("環境変数 BUFFER_API_KEY が設定されていません（BufferのAPIキーを設定してください）")
    req = urllib.request.Request(
        BUFFER_ENDPOINT,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        body = json.loads(res.read().decode())
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"], ensure_ascii=False))
    return body["data"]


def cmd_channels(args):
    data = buffer_call(LIST_CHANNELS)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print("\nXのチャンネルの id を brands/<ブランド>/buffer.json の channel_id に書いてください。")


def cmd_push(args):
    bdir = brand_dir(args.brand)
    path = bdir / args.week
    posts = parse_week(path)
    errors, _ = check(bdir, posts)
    if any(errors.values()):
        sys.exit("validate でエラーがあるため送信しません。先に修正してください。")
    targets = sorted(approved(posts), key=lambda p: p["datetime"])
    if not targets:
        sys.exit("送信対象（status: 承認）の投稿がありません。")
    if args.limit:
        targets = targets[: args.limit]
    conf_path = bdir / "buffer.json"
    channel = json.loads(conf_path.read_text(encoding="utf-8")).get("channel_id") if conf_path.exists() else ""
    if not channel and not args.dry_run:
        sys.exit(f"{conf_path.relative_to(ROOT)} に channel_id がありません。先に channels で確認してください。")
    mode = "予約" if args.schedule else "下書き"
    print(f"Bufferへ{mode}として {len(targets)}本を登録します{'（dry-run：送信しません）' if args.dry_run else ''}\n")
    for p in targets:
        due = parse_when(p).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {"text": p["text"], "channelId": channel or "(未設定)", "schedulingType": "automatic",
                   "mode": "customScheduled", "dueAt": due, "saveToDraft": not args.schedule}
        if args.dry_run:
            print(f"--- {p['id']} {p['datetime']} JST\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
            continue
        try:
            res = buffer_call(CREATE_POST, {"input": payload})["createPost"]
        except Exception as e:
            print(f"NG {p['id']}: {e}\n残りの送信を中止しました。")
            break
        if res.get("message") or not res.get("post"):
            print(f"NG {p['id']}: {res.get('message') or res}\n残りの送信を中止しました。")
            break
        set_fields(path, p["id"], status="登録済", buffer_id=res["post"]["id"], buffer_mode=mode)
        print(f"OK {p['id']} → Buffer {res['post']['id']}（{mode}）")


def cmd_report(args):
    bdir = brand_dir(args.brand)
    p = bdir / "実績.csv"
    if not p.exists():
        sys.exit("実績.csv がありません。")
    with p.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("投稿ID")]
    if not rows:
        sys.exit("実績.csv にデータがありません。")

    def num(r, k):
        try:
            return float(r.get(k) or 0)
        except ValueError:
            return 0.0

    L = ["# X投稿 実績レポート", "", f"生成: {dt.datetime.now(JST):%Y-%m-%d %H:%M}　対象: {len(rows)}投稿", ""]
    groups = defaultdict(list)
    for r in rows:
        groups[r.get("種類", "")].append(r)
    L += ["## 種類別の平均", "", "| 種類 | 本数 | 表示 | いいね | リポスト | 返信 | リンククリック | 反応率 |", "|---|---|---|---|---|---|---|---|"]
    for k, rs in groups.items():
        imp = sum(num(r, "表示数") for r in rs)
        eng = sum(num(r, "いいね") + num(r, "リポスト") + num(r, "返信") for r in rs)
        avg = lambda c: sum(num(r, c) for r in rs) / len(rs)
        L.append(f"| {k} | {len(rs)} | {avg('表示数'):.0f} | {avg('いいね'):.1f} | {avg('リポスト'):.1f} | "
                 f"{avg('返信'):.1f} | {avg('リンククリック'):.1f} | {(eng / imp * 100 if imp else 0):.1f}% |")
    themes = defaultdict(list)
    for r in rows:
        themes[r.get("テーマ", "")].append(num(r, "表示数"))
    L += ["", "## テーマ別の平均表示数（多い順）", ""]
    for t, v in sorted(themes.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        L.append(f"- {t or '(未記入)'}: {sum(v) / len(v):.0f}（{len(v)}本）")
    L += ["", "## 表示数の上位5投稿", ""]
    for r in sorted(rows, key=lambda r: -num(r, "表示数"))[:5]:
        L.append(f"- {r.get('投稿日時', '')} [{r.get('種類', '')}] {r.get('テーマ', '')}：表示 {num(r, '表示数'):.0f} / クリック {num(r, 'リンククリック'):.0f}")

    sp = bdir / "note売上.csv"
    if sp.exists():
        with sp.open(encoding="utf-8-sig") as f:
            sales = [s for s in csv.DictReader(f) if s.get("週")]
        clicks = defaultdict(float)
        for r in rows:
            d = dt.date.fromisoformat(r["投稿日時"][:10])
            clicks[f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"] += num(r, "リンククリック")
        L += ["", "## 週ごとのリンククリックとnote売上", "",
              "※投稿と購入を1件ずつ結びつけることはできないため、週単位の傾向として見てください。", "",
              "| 週 | リンククリック | 販売件数 | 売上(円) |", "|---|---|---|---|"]
        for s in sales:
            L.append(f"| {s['週']} | {clicks.get(s['週'], 0):.0f} | {s.get('販売件数', '')} | {s.get('売上', '')} |")
    out = bdir / "分析" / "実績レポート.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"{out.relative_to(ROOT)} を作成しました。")


def main():
    ap = argparse.ArgumentParser(description="X投稿 半自動化ツール")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "export", "push"):
        p = sub.add_parser(name)
        p.add_argument("--brand", required=True)
        p.add_argument("week", help="ブランドフォルダからの相対パス（例: 週次/2026-W44.md）")
        if name == "push":
            p.add_argument("--dry-run", action="store_true", help="送信内容を表示するだけ")
            p.add_argument("--schedule", action="store_true", help="下書きではなく予約投稿として登録する")
            p.add_argument("--limit", type=int, default=0, help="送信する最大本数（最初の確認は 1 を推奨）")
    sub.add_parser("channels")
    p = sub.add_parser("report")
    p.add_argument("--brand", required=True)
    args = ap.parse_args()
    if args.cmd == "validate":
        sys.exit(1 if cmd_validate(args) else 0)
    {"export": cmd_export, "channels": cmd_channels, "push": cmd_push, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
