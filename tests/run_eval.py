"""跑评测：先规则引擎基线，再用 v1/v2 prompt 各跑一次，存进 DB。"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8210"


def call(method, path, token=None, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw), e.code
        except json.JSONDecodeError:
            return {"raw": raw}, e.code


def show(tag, r):
    print(f"\n===== {tag} =====")
    print(f"  用例数={r['total_cases']}  埋雷={r['planted_total']}  抓到={r['caught_total']}  误报={r['false_positive_total']}")
    print(f"  召回率 recall = {r['recall']*100:.1f}%")
    print(f"  误报率 fpr    = {r['false_positive_rate']*100:.1f}%")
    print(f"  F1            = {r['f1']*100:.1f}%")
    print(f"  平均耗时      = {r['avg_latency_ms']}ms   总token={r['total_tokens']}")
    print(f"  模型={r['model_name']}  标签={r['label']}  run_id={r['id']}")
    detail = r.get("detail") or {}
    print(f"  precision={detail.get('precision')}  总耗时={detail.get('elapsed_sec')}s")
    bad = [c for c in detail.get("cases", []) if c["caught"] < c["planted"]]
    if bad:
        print(f"  有漏报的用例 {len(bad)} 个：")
        for c in bad:
            print(f"    {c['case_id']} {c['title'][:24]}  抓到 {c['caught']}/{c['planted']}  漏={c['missed']}")


r, code = call("POST", "/api/auth/login", body={"username": "admin", "password": "demo123456"})
tok = r["access_token"]
print("登录 admin OK")

# ---------- 1. 规则引擎基线 ----------
t0 = time.time()
r, code = call("POST", "/api/eval/run?rules_only=true&label=" + urllib.parse.quote("规则引擎基线（降级模式）"), token=tok)
if code != 200:
    print(f"规则引擎基线失败 {code}: {r}")
else:
    show("规则引擎基线", r)
print(f"（耗时 {time.time()-t0:.0f}s）")

# ---------- 2. 拿 prompt 版本 ----------
r, code = call("GET", "/api/prompts", token=tok)
versions = {p["version"]: p["id"] for p in r}
print(f"\nprompt 版本：{versions}")

# ---------- 3. v1 跑分 ----------
t0 = time.time()
r, code = call("POST", f"/api/eval/run?prompt_version_id={versions[1]}&label=" + urllib.parse.quote("v1 初版 prompt"),
               token=tok)
if code != 200:
    print(f"v1 跑分失败 {code}: {r}")
else:
    show("v1 初版 prompt", r)
print(f"（耗时 {time.time()-t0:.0f}s）")

# ---------- 4. v2 跑分 ----------
t0 = time.time()
r, code = call("POST", f"/api/eval/run?prompt_version_id={versions[2]}&label=" + urllib.parse.quote("v2 强化 prompt"),
               token=tok)
if code != 200:
    print(f"v2 跑分失败 {code}: {r}")
else:
    show("v2 强化 prompt", r)
print(f"（耗时 {time.time()-t0:.0f}s）")

# ---------- 5. 历史 ----------
r, code = call("GET", "/api/eval/runs", token=tok)
print("\n===== 历史跑分 =====")
for x in r:
    print(f"  #{x['id']}  {x['label']:24s} recall={x['recall']*100:5.1f}%  fpr={x['false_positive_rate']*100:5.1f}%  f1={x['f1']*100:5.1f}%")
