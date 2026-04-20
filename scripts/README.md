# Pre-plan spike

按 `docs/plans/2026-04-20-001-feat-google-serp-analyzer-mvp-plan.md` 的 **Pre-plan gate** 要求执行。

## 一次性环境准备

```bash
cd "$(git rev-parse --show-toplevel)/SEOSERPER"
python3 -m venv .venv
source .venv/bin/activate
pip install playwright
playwright install chromium
```

## 3 天执行节奏（plan 要求: ≥60 query，分散 3 天）

每天 3 个 burst，每 burst 3-5 query，burst 内节奏 1-2 min，burst 之间数小时间隔：

```bash
# burst 示例（大约 5-8 min 完成）
python scripts/spike.py run --limit 5 --pacing 90
```

其它选项：
- `--locale en-us|zh-cn|ja-jp` 限定单一 locale（默认混合随机）
- `--pacing 60` 更紧节奏（不推荐，会推高阻断率噪声）
- `--no-headless` 有头模式调试 selector

每次 run 都 append 到 `scripts/spike_results.jsonl`，HTML 落 `tests/fixtures/serp/{locale}/`。

## 判定

累计 ≥60 query 后：

```bash
python scripts/spike.py analyze
```

输出按 plan 阈值分档：
- 0 blocked → SHIP
- 1-2 blocked → SHIP (baseline noted)
- 3-5 blocked → NOTE-AND-SHIP
- ≥6 blocked → 回 `/ce:brainstorm`（不是 drop Playwright，是重评 proxy）

## Fixture 选取

spike 结束后从 `tests/fixtures/serp/{locale}/` 挑出 3 份 `status=ok` 的 HTML 作为 Unit 4 parser 的回归 fixture，重命名为 `en-us.html` / `zh-cn.html` / `ja-jp.html`。其余保留在目录内供 selector 漂移对照。

Suggest JSON contract fixture（plan 要求 2 份）单独在 `tests/fixtures/suggest/` 手工捕获：

```bash
curl -sG 'https://suggestqueries.google.com/complete/search' \
  --data-urlencode 'client=firefox' \
  --data-urlencode 'q=best running shoes' \
  --data-urlencode 'hl=en' \
  --data-urlencode 'gl=us' \
  > tests/fixtures/suggest/en-us-ok.json
```
