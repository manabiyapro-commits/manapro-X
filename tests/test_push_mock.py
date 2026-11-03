"""予約枠つき push の動作確認（ローカルの模擬 Buffer。実際の Buffer には接続しない）
実行: python3 tests/test_push_mock.py（リポジトリの直下で）"""
import datetime as dt, json, os, re, shutil, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

XP = Path(__file__).resolve().parent.parent
JST = dt.timezone(dt.timedelta(hours=9))
LIMIT = 10
queue = [{"id": "d0", "status": "draft", "dueAt": None, "text": "既存の下書き", "channelId": "chX"}]
creates = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["query"]
        if "GetOrganizations" in q:
            data = {"account": {"organizations": [{"id": "org1", "name": "m"}]}}
        elif "GetChannels" in q:
            data = {"channels": [{"id": "chX", "name": "manabiyapro", "displayName": "manabiyapro", "service": "twitter", "descriptor": "", "isQueuePaused": False}]}
        elif "GetLimits" in q:
            data = {"account": {"organizations": [{"id": "org1", "limits": {"scheduledPosts": LIMIT}}]}}
        elif "GetQueue" in q:
            data = {"posts": {"edges": [{"node": n} for n in queue]}}
        elif "createPost" in q:
            used = sum(1 for n in queue if n["status"] in ("scheduled", "draft"))
            if used >= LIMIT:
                data = {"createPost": {"message": "LimitReached"}}
            else:
                text = json.JSONDecoder().raw_decode(q[q.index("text: ") + 6:])[0]
                due = re.search(r'dueAt: "([^"]+)"', q).group(1)
                n = {"id": f"p{len(creates) + 1}", "status": "scheduled", "dueAt": due, "text": text, "channelId": "chX"}
                queue.append(n); creates.append(n)
                data = {"createPost": {"post": {"id": n["id"], "status": "scheduled", "dueAt": due}}}
        else:
            data = {}
        b = json.dumps({"data": data}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(b)


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
EP = f"http://127.0.0.1:{srv.server_port}"

T = XP / "brands" / "_t"
W = "週次/TEST.md"
sys.path.insert(0, str(XP / "tools")); import x_tool as t

# 実行日に左右されないよう、テスト用の週（2週間後の月曜〜日曜、14本）を毎回つくる
MON = (dt.datetime.now(JST) + dt.timedelta(days=14 - dt.datetime.now(JST).weekday())).date()
TEXTS = ["春の桜が咲く公園で読書", "夏祭りの太鼓が響く夜道", "秋の紅葉を眺める山道", "冬の雪景色と温かい鍋",
         "朝焼けの海辺を散歩する", "図書館で静かに調べもの", "駅前のパン屋で朝ごはん", "川沿いを自転車で走る",
         "星空の下でキャンプする", "雨音を聞きながら昼寝", "動物園でキリンを見学", "美術館で絵画を鑑賞する",
         "畑でトマトを収穫した日", "港の灯台から船を眺める"]
UNAPPROVED = {"TEST-2", "TEST-11"}


def slot(i):  # i=1..14 → (日時文字列)
    d = MON + dt.timedelta(days=(i - 1) // 2)
    return f"{d:%Y-%m-%d} {'07:30' if i % 2 else '20:00'}"


def fresh():
    shutil.rmtree(T, ignore_errors=True)
    shutil.copytree(XP / "brands" / "manabiya-project", T)
    for f in (T / "週次").glob("*.md"):
        f.unlink()
    (T / "登録記録.csv").unlink(missing_ok=True)
    (T / "buffer.json").write_text(json.dumps({"expected_handle": "manabiyapro", "organization_id": "", "channel_id": ""}))
    lines = ["# テスト週"]
    for i in range(1, 15):
        lines += ["", f"## {i}", f"- id: TEST-{i}", f"- datetime: {slot(i)}", "- kind: 情報提供", f"- theme: テスト{i}",
                  "- status: 未承認", "```text", TEXTS[i - 1], "```"]
    (T / W).write_text("\n".join(lines) + "\n", encoding="utf-8")
    for p in t.parse_week(T / W):
        if p["id"] not in UNAPPROVED:
            t.set_fields(T / W, p["id"], status="承認", approved_hash=t.approval_hash(p))
    queue[:] = [{"id": "d0", "status": "draft", "dueAt": None, "text": "既存の下書き", "channelId": "chX"}]


def run(*a):
    code = ("import sys; sys.path.insert(0,'tools'); import x_tool as t; "
            f"t.BUFFER_ENDPOINT={EP!r}; sys.argv=['x']+{list(a)!r}; t.main()")
    r = subprocess.run([sys.executable, "-c", code], cwd=XP, env=dict(os.environ, BUFFER_API_KEY="k"), capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print(f"$ {' '.join(a[:1])} … (exit {r.returncode})\n" + "\n".join(l for l in out.splitlines() if not l.startswith((" →", "   [", "Bufferに連携", "OK 投稿先")) and l.strip()) + "\n")
    return r.returncode


ok = True
def expect(cond, msg):
    global ok
    print(("  ✔ " if cond else "  ✘ ") + msg); ok &= bool(cond)


def utc(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=JST).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


fresh()
print("## 1. dry-run：予約枠の確認（上限10、下書き1件を含めて空き9）")
run("push", "--brand", "_t", W, "--mode", "schedule", "--dry-run")
expect(len(creates) == 0, "dry-run では何も登録されない")

print("## 2. 1回目の登録：早い順に9本、残りは枠待ち。未承認は対象外")
run("push", "--brand", "_t", W, "--mode", "schedule")
reg = [r["投稿ID"] for r in t.load_ledger(T)]
expect(len(creates) == 9, f"9本が登録された（{len(creates)}本）")
dues = [c["dueAt"] for c in creates]
expect(dues == sorted(dues), "投稿日時が早い順に登録された")
expect(not (UNAPPROVED & set(reg)), "未承認の投稿は登録されない")
expect(creates[0]["dueAt"] == utc(slot(1)), "最初の登録は、いちばん早い承認済み投稿")
st = {p["id"]: p for p in t.parse_week(T / W)}
expect(st["TEST-1"].get("buffer_status", "").startswith("登録済"), "登録した投稿には buffer_status: 登録済 が記録される")

print("## 3. もう一度実行：枠がないので何も登録されない（重複もしない）")
before = len(creates)
run("push", "--brand", "_t", W, "--mode", "schedule")
expect(len(creates) == before, "空きがないときは登録しない")

print("## 4. 4本が投稿された想定（予約→投稿済み）→ 追加登録で残りが入る")
for n in [n for n in queue if n["status"] == "scheduled"][:4]:
    n["status"] = "sent"
run("push", "--brand", "_t", W, "--mode", "schedule")
reg = [r["投稿ID"] for r in t.load_ledger(T)]
expect(len(creates) == 12, f"承認済み12本の残り3本が登録され、合計12本になった（{len(creates)}本）")
expect(len(set(reg)) == len(reg), "同じ投稿IDの二重登録はない")

print("## 5. 承認後に日時だけ変更（翌週の月曜 7:30 へ）→ その投稿は登録されない")
fresh(); n0 = len(creates)
moved = f"{MON + dt.timedelta(days=7):%Y-%m-%d} 07:30"
(T / W).write_text((T / W).read_text().replace(f"- datetime: {slot(1)}", f"- datetime: {moved}", 1))
run("push", "--brand", "_t", W, "--mode", "schedule")
ids = [r["投稿ID"] for r in t.load_ledger(T)]
expect("TEST-1" not in ids, "日時を変えた投稿は登録されない（再承認が必要）")
expect(len(creates) - n0 == 9, f"他の承認済み投稿は通常どおり登録される（{len(creates) - n0}本）")

print("## 6. 枠にない時刻（19:30）→ validate エラー")
fresh()
(T / W).write_text((T / W).read_text().replace(f"- datetime: {slot(2)}", f"- datetime: {slot(2)[:11]}19:30", 1))
expect(run("validate", "--brand", "_t", W) == 1, "19:30 はエラーになる")

print("## 7. 承認済みで、予定日時が過ぎた投稿（昨日 7:30）→ 登録されずスキップ")
fresh(); n0 = len(creates)
past = f"{(dt.datetime.now(JST) - dt.timedelta(days=1)):%Y-%m-%d} 07:30"
(T / W).write_text((T / W).read_text().replace(f"- datetime: {slot(1)}", f"- datetime: {past}", 1))
pp = {x["id"]: x for x in t.parse_week(T / W)}["TEST-1"]
t.set_fields(T / W, "TEST-1", approved_hash=t.approval_hash(pp))  # 承認は有効のまま、日時だけが過去
run("push", "--brand", "_t", W, "--mode", "schedule")
ids = [r["投稿ID"] for r in t.load_ledger(T)]
expect("TEST-1" not in ids, "過去日時の投稿は登録されない")
now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")
expect(all(c["dueAt"] > now_utc for c in creates[n0:]), "登録されたのは未来日時の投稿だけ")

shutil.rmtree(T)
print("\n結果:", "すべて成功" if ok else "失敗あり")
sys.exit(0 if ok else 1)
