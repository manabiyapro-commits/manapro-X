#!/usr/bin/env python3
"""X投稿 半自動化ツール（標準ライブラリのみ）

使い方（x-posting/ で実行。W=週次/2026-W44.md）:
  python3 tools/x_tool.py check    --brand manabiya-project          # 接続確認と投稿先 @manabiyapro の確認
  python3 tools/x_tool.py posts    --brand manabiya-project          # Bufferの予約・下書き一覧
  python3 tools/x_tool.py validate --brand manabiya-project $W       # 投稿案のチェック
  python3 tools/x_tool.py review   --brand manabiya-project $W       # 承認用の一覧
  python3 tools/x_tool.py approve  --brand manabiya-project $W 2026W44-1 ...
  python3 tools/x_tool.py push     --brand manabiya-project $W --mode schedule --dry-run
  python3 tools/x_tool.py push     --brand manabiya-project $W --mode schedule
  python3 tools/x_tool.py results  --brand manabiya-project          # 登録結果の一覧
  python3 tools/x_tool.py export   --brand manabiya-project $W       # API を使わずコピペ用に書き出す
  python3 tools/x_tool.py report   --brand manabiya-project

安全のための約束:
  - Bufferに送るのは approve で承認した投稿だけ。承認後に本文が変わったら、承認し直すまで送らない。
  - 送る前に毎回、投稿先が buffer.json の expected_handle（@manabiyapro）のXチャンネルであることを確認する。
  - 登録記録.csv と Buffer上の予約・下書きを照合し、同じ投稿を二重に登録しない。
  - Bufferのパスワードは使わない。APIキーは環境変数 BUFFER_API_KEY で渡し、ファイルに保存しない。
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
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
         "合格率", "裏ワザ", "知らないと損", "人生が変わる", "爆速", "圧倒的",
         # 自治体で違う制度を断定する表現・根拠を示しにくい出題予想
         "合格すれば", "有利になる", "可能性が低い", "出題されない", "出ない"]

# Buffer GraphQL（developers.buffer.com の Your First Post / Get Channels / Get Posts For Channels /
# Posts & Scheduling / Create Draft Post に基づく）。入力型の名前に依存しないよう、値はクエリに直接埋め込む。
GET_ORGS = "query GetOrganizations { account { organizations { id name } } }"
GET_CHANNELS = """query GetChannels {
  channels(input: { organizationId: %s }) { id name displayName service descriptor isQueuePaused }
}"""
GET_POSTS = """query GetPosts {
  posts(first: %d, input: { organizationId: %s, filter: { status: [%s], channelIds: [%s] } }) {
    edges { node { id status dueAt text } }
  }
}"""
CREATE_POST = """mutation CreatePost {
  createPost(input: { text: %s, channelId: %s, schedulingType: automatic, mode: customScheduled,
                      dueAt: %s, saveToDraft: %s }) {
    ... on PostActionSuccess { post { id status dueAt } }
    ... on MutationError { message }
  }
}"""
X_SERVICES = ("twitter", "x")
LEDGER_FIELDS = ["登録日時", "投稿ID", "予定日時(JST)", "登録モード", "Buffer投稿ID", "Bufferの状態", "本文ハッシュ", "本文"]


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
    """投稿履歴.csv と、history で取得した 分析/過去投稿.csv の両方を、重複チェックの対象にする"""
    rows = []
    for p, date_col in ((bdir / "投稿履歴.csv", "投稿日時"), (bdir / "分析" / "過去投稿.csv", "投稿日時(JST)")):
        if p.exists():
            with p.open(encoding="utf-8-sig") as f:
                rows += [{"本文": r.get("本文", ""), "投稿日時": r.get(date_col, "")} for r in csv.DictReader(f)]
    return rows


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


def gql(s):
    """Python の文字列を GraphQL の文字列リテラルにする（JSON のエスケープと互換）"""
    return json.dumps(s, ensure_ascii=False)


def buffer_call(query):
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    if not key:
        sys.exit("環境変数 BUFFER_API_KEY が設定されていません。README の「APIキーの設定」を見てください。")
    req = urllib.request.Request(
        BUFFER_ENDPOINT,
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        hint = {401: "APIキーが無効か期限切れです", 403: "権限がないか、ネットワークで遮断されています",
                429: "リクエスト数の上限に達しました。時間をおいてください"}.get(e.code, "")
        sys.exit(f"Buffer API エラー: HTTP {e.code} {hint}\n{e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Buffer API に接続できません: {e.reason}\n"
                 "（クラウド環境の場合、Network access の許可ドメインに api.buffer.com が必要です）")
    if body.get("errors"):
        sys.exit("Buffer API がエラーを返しました:\n" + json.dumps(body["errors"], ensure_ascii=False, indent=2))
    return body["data"]


def norm_handle(s):
    return (s or "").strip().lstrip("@").lower()


def load_buffer_conf(bdir):
    p = bdir / "buffer.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def verify_channel(bdir):
    """buffer.json の expected_handle と一致するXチャンネルを1つだけ特定する。結果は buffer.json に保存"""
    conf = load_buffer_conf(bdir)
    want = norm_handle(conf.get("expected_handle"))
    if not want:
        sys.exit("buffer.json に expected_handle（投稿先のXアカウント名）がありません。")
    orgs = buffer_call(GET_ORGS)["account"]["organizations"]
    found, all_channels = [], []
    for org in orgs:
        for ch in buffer_call(GET_CHANNELS % gql(org["id"]))["channels"]:
            all_channels.append((org, ch))
            names = {norm_handle(ch.get("name")), norm_handle(ch.get("displayName"))}
            if str(ch.get("service", "")).lower() in X_SERVICES and want in names:
                found.append((org, ch))
    print("Bufferに連携されているチャンネル:")
    for org, ch in all_channels:
        mark = "→" if (org, ch) in found else " "
        print(f" {mark} [{ch.get('service')}] name={ch.get('name')} displayName={ch.get('displayName')} "
              f"id={ch['id']}{'（キュー停止中）' if ch.get('isQueuePaused') else ''}")
    if not all_channels:
        sys.exit("\nチャンネルが1つもありません。BufferでXアカウントを連携してください。")
    if len(found) != 1:
        sys.exit(f"\n投稿先「@{want}」のXチャンネルを{'特定できません（複数一致）' if found else '見つけられません'}。"
                 "安全のため、ここで止めます。")
    org, ch = found[0]
    if conf.get("channel_id") and conf["channel_id"] != ch["id"]:
        sys.exit(f"\nbuffer.json の channel_id（{conf['channel_id']}）と、実際のチャンネル（{ch['id']}）が違います。"
                 "連携し直した可能性があります。確認のうえ buffer.json の channel_id を空にして再実行してください。")
    conf.update(organization_id=org["id"], channel_id=ch["id"], verified_at=dt.datetime.now(JST).isoformat(timespec="seconds"))
    (bdir / "buffer.json").write_text(json.dumps(conf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nOK 投稿先は X の @{want} です（channel_id={ch['id']}）。")
    return conf


def fetch_posts(conf, statuses, first=50):
    q = GET_POSTS % (first, gql(conf["organization_id"]), ", ".join(statuses), gql(conf["channel_id"]))
    return [e["node"] for e in buffer_call(q)["posts"]["edges"]]


def cmd_check(args):
    """接続確認：APIキー → 組織 → チャンネル → 投稿先 @アカウント の確認"""
    verify_channel(brand_dir(args.brand))


def cmd_posts(args):
    bdir = brand_dir(args.brand)
    conf = verify_channel(bdir)
    statuses = args.status.split(",")
    rows = fetch_posts(conf, statuses)
    rows.sort(key=lambda n: n.get("dueAt") or "")
    print(f"\nBufferの投稿一覧（状態: {', '.join(statuses)}）: {len(rows)}件")
    for n in rows:
        due = n.get("dueAt")
        jst = dt.datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone(JST).strftime("%m/%d %H:%M") if due else "日時なし"
        text = re.sub(r"\s+", " ", n.get("text") or "")
        print(f"  {jst}  [{n.get('status')}]  {text[:50]}{'…' if len(text) > 50 else ''}  (id={n['id']})")


def text_hash(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()[:12]


def load_ledger(bdir):
    p = bdir / "登録記録.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def append_ledger(bdir, row):
    p = bdir / "登録記録.csv"
    new = not p.exists()
    with p.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def cmd_review(args):
    """承認用の一覧（日時・状態・本文）を表示する"""
    bdir = brand_dir(args.brand)
    posts = parse_week(bdir / args.week)
    errors, warns = check(bdir, posts)
    for p in sorted(posts, key=lambda p: p.get("datetime", "")):
        pid = p.get("id", "?")
        state = p.get("status")
        if state == "承認" and p.get("approved_hash") != text_hash(p["text"]):
            state = "承認（※承認後に本文が変わっています）"
        print("=" * 60)
        print(f"{pid}　{p.get('datetime')}（日本時間）　{p.get('kind')}　状態: {state}")
        print("-" * 60)
        print(p["text"])
        for m in errors.get(pid, []):
            print(f"  ⚠ エラー: {m}")
        for m in warns.get(pid, []):
            print(f"  △ 注意: {m}")
    print("=" * 60)
    print("承認するには: python3 tools/x_tool.py approve --brand", args.brand, args.week, "<投稿ID> ...")


def cmd_approve(args):
    """指定した投稿を承認する。承認時点の本文のハッシュを記録し、以後本文が変わったら承認は無効になる"""
    bdir = brand_dir(args.brand)
    path = bdir / args.week
    posts = {p.get("id"): p for p in parse_week(path)}
    errors, _ = check(bdir, list(posts.values()))
    for pid in args.ids:
        p = posts.get(pid)
        if not p:
            print(f"NG {pid}: その投稿IDはありません")
        elif p.get("status") == "登録済":
            print(f"NG {pid}: すでにBufferに登録済みです")
        elif errors.get(pid):
            print(f"NG {pid}: エラーがあるため承認できません（{'; '.join(errors[pid])}）")
        else:
            set_fields(path, pid, status="承認", approved_hash=text_hash(p["text"]),
                       approved_at=dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M"))
            print(f"OK {pid} を承認しました")


def cmd_push(args):
    bdir = brand_dir(args.brand)
    path = bdir / args.week
    posts = parse_week(path)
    errors, _ = check(bdir, posts)
    if any(errors.values()):
        sys.exit("validate でエラーがあるため送信しません。先に修正してください。")
    targets = []
    for p in sorted(approved(posts), key=lambda p: p["datetime"]):
        if p.get("approved_hash") != text_hash(p["text"]):
            sys.exit(f"{p['id']} は承認後に本文が変わっています（または approve コマンドで承認されていません）。"
                     "review で確認し、approve で承認し直してください。")
        targets.append(p)
    if args.only:
        targets = [p for p in targets if p["id"] in args.only]
    if not targets:
        sys.exit("送信対象（approve で承認済み）の投稿がありません。")

    ledger = load_ledger(bdir)
    done_ids = {r["投稿ID"] for r in ledger}
    done_hashes = {r["本文ハッシュ"] for r in ledger}
    skipped = [p for p in targets if p["id"] in done_ids or text_hash(p["text"]) in done_hashes]
    targets = [p for p in targets if p not in skipped]
    for p in skipped:
        print(f"スキップ {p['id']}: 登録記録.csv にすでに登録があります（重複登録の防止）")
    if not targets:
        print("新しく登録する投稿はありません。")
        return

    draft = args.mode == "draft"
    label = "下書き" if draft else "予約投稿"
    if args.dry_run:
        print(f"\n[dry-run] Bufferへ{label}として登録する予定の投稿: {len(targets)}本（送信しません）")
        for p in targets:
            print(f"  {p['datetime']} JST  {p['id']}  {re.sub(chr(10), ' ', p['text'])[:40]}…")
        return

    conf = verify_channel(bdir)  # 毎回、投稿先が @expected_handle であることを確認する
    existing = {text_hash(n.get("text") or "") for n in fetch_posts(conf, ["scheduled", "draft"], 100)}
    results = []
    for p in targets:
        h = text_hash(p["text"])
        if h in existing:
            results.append((p, "スキップ", "Bufferに同じ本文の投稿がすでにあります"))
            continue
        due = parse_when(p).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        q = CREATE_POST % (gql(p["text"]), gql(conf["channel_id"]), gql(due), "true" if draft else "false")
        res = buffer_call(q)["createPost"]
        if res.get("message") or not res.get("post"):
            results.append((p, "失敗", res.get("message") or json.dumps(res, ensure_ascii=False)))
            print("失敗したため、残りの送信を中止します。")
            break
        post = res["post"]
        set_fields(path, p["id"], status="登録済", buffer_id=post["id"], buffer_mode=label)
        append_ledger(bdir, {"登録日時": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M"), "投稿ID": p["id"],
                             "予定日時(JST)": p["datetime"], "登録モード": label, "Buffer投稿ID": post["id"],
                             "Bufferの状態": post.get("status", ""), "本文ハッシュ": h, "本文": p["text"]})
        existing.add(h)
        results.append((p, "成功", f"Buffer id={post['id']} 状態={post.get('status')}"))
    print(f"\n登録結果（{label}）")
    for p, r, d in results:
        print(f"  [{r}] {p['datetime']} JST  {p['id']}  {d}")
    print("\n全登録履歴: python3 tools/x_tool.py results --brand", args.brand)


GET_SENT = """query GetSent {
  posts(first: 100%s, input: { organizationId: %s, filter: { status: [sent], channelIds: [%s] } }) {
    edges { node { id text sentAt dueAt via externalLink metrics { type value } metricsUpdatedAt } }
    pageInfo { hasNextPage endCursor }
  }
}"""
METRIC_COLS = [("impressions", "表示回数"), ("reactions", "いいね"), ("reposts", "リポスト"),
               ("comments", "返信"), ("clicks", "クリック"), ("engagementRate", "エンゲージメント率(%)")]


def cmd_history(args):
    """Bufferから投稿済みのXの投稿と指標を取得し、分析/過去投稿.csv に保存する（読み取りのみ）"""
    bdir = brand_dir(args.brand)
    conf = verify_channel(bdir)
    posts, after = [], None
    for _ in range(args.max_pages):
        page = buffer_call(GET_SENT % (f", after: {gql(after)}" if after else "",
                                      gql(conf["organization_id"]), gql(conf["channel_id"])))["posts"]
        posts += [e["node"] for e in page["edges"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    out = bdir / "分析" / "過去投稿.csv"
    out.parent.mkdir(exist_ok=True)
    rows = []
    for p in posts:
        m = {x["type"]: x["value"] for x in p.get("metrics") or []}
        when = dt.datetime.fromisoformat((p.get("sentAt") or p["dueAt"]).replace("Z", "+00:00")).astimezone(JST)
        rows.append([when.strftime("%Y-%m-%d %H:%M"), *[m.get(k, "") for k, _ in METRIC_COLS],
                     "Buffer" if p.get("via") == "buffer" else "X直接", p.get("externalLink") or "",
                     (p.get("metricsUpdatedAt") or "")[:10], p["text"]])
    rows.sort()
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["投稿日時(JST)", *[n for _, n in METRIC_COLS], "投稿元", "URL", "指標の更新日", "本文"])
        w.writerows(rows)
    print(f"\n{len(rows)}件を {out.relative_to(ROOT)} に保存しました"
          + (f"（{rows[0][0][:10]}〜{rows[-1][0][:10]}）" if rows else ""))


def cmd_results(args):
    rows = load_ledger(brand_dir(args.brand))
    if not rows:
        print("まだBufferに登録した投稿はありません。")
    for r in rows:
        print(f"{r['登録日時']} 登録 / 予定 {r['予定日時(JST)']} / {r['投稿ID']} / {r['登録モード']} / {r['Buffer投稿ID']} / {r['Bufferの状態']}")


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
    for name in ("validate", "review", "approve", "export", "push"):
        p = sub.add_parser(name)
        p.add_argument("--brand", required=True)
        p.add_argument("week", help="ブランドフォルダからの相対パス（例: 週次/2026-W44.md）")
        if name == "approve":
            p.add_argument("ids", nargs="+", help="承認する投稿ID（例: 2026W44-1 2026W44-3）")
        if name == "push":
            p.add_argument("--mode", required=True, choices=("schedule", "draft"),
                           help="schedule=予約投稿 / draft=Bufferの下書き（必ずどちらかを指定）")
            p.add_argument("--only", nargs="+", help="送信する投稿IDを限定する")
            p.add_argument("--dry-run", action="store_true", help="送信せず、送る予定の投稿を表示するだけ")
    for name in ("check", "posts", "history", "results", "report"):
        p = sub.add_parser(name)
        p.add_argument("--brand", required=True)
        if name == "posts":
            p.add_argument("--status", default="scheduled,draft", help="カンマ区切り（scheduled, draft, sent など）")
        if name == "history":
            p.add_argument("--max-pages", type=int, default=10, help="取得する最大ページ数（1ページ100件）")
    args = ap.parse_args()
    if args.cmd == "validate":
        sys.exit(1 if cmd_validate(args) else 0)
    {"review": cmd_review, "approve": cmd_approve, "export": cmd_export, "check": cmd_check, "posts": cmd_posts,
     "history": cmd_history, "push": cmd_push, "results": cmd_results, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
