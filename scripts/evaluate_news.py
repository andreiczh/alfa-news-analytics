"""Reproducible editorial reference; labels never enter the ranking input."""

import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess

from alfa_agent.news_ranking import rank_news


def evaluate(rank, data):
    now = datetime.fromisoformat(data["as_of"])
    items = [
        {
            "news_id": "news_" + hashlib.sha256(e["id"].encode()).hexdigest()[:24],
            "reference_id": e["id"],
            "url": "https://benchmark.invalid/" + e["id"],
            "title": e["title"],
            "excerpt": e["title"],
            "text": e["title"],
            "published_at": None
            if e.get("unknown_date")
            else (now - timedelta(days=e.get("age_days", 1))).isoformat(),
            "publisher": "Synthetic reference only",
            "kind": "public_page",
            "read_status": "article_read",
        }
        for e in data["events"]
    ]
    rows = []
    for company, context in data["profiles"].items():
        for role in data["roles"]:
            selected, excluded = rank(items, context, role, now)
            relevant = {e["id"] for e in data["events"] if role in e.get("labels", {}).get(company, [])}
            critical = {e["id"] for e in data["events"] if company in e.get("critical_for", [])}
            top = [e["reference_id"] for e in selected[:5]]
            all_ids = {e["reference_id"] for e in selected}
            rows.append(
                {
                    "company": company,
                    "role": role,
                    "precision_at_5": len(set(top) & relevant) / 5,
                    "relevant_recall_at_20": len(all_ids & relevant) / len(relevant),
                    "critical_recall_at_20": len(all_ids & critical) / len(critical),
                    "top_5": top,
                    "irrelevant_at_5": sorted(set(top) - relevant),
                    "missed_critical": sorted(critical - all_ids),
                    "exclusions": excluded,
                }
            )
    return {
        "cases": rows,
        "mean_precision_at_5": sum(r["precision_at_5"] for r in rows) / len(rows),
        "minimum_precision_at_5": min(r["precision_at_5"] for r in rows),
        "minimum_critical_recall_at_20": min(r["critical_recall_at_20"] for r in rows),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", default="data/quality/news-reference-v1.json")
    parser.add_argument("--output", default="reports/v6/news-reference.json")
    parser.add_argument("--baseline-commit")
    args = parser.parse_args()
    path = Path(args.reference)
    data = json.loads(path.read_text())
    result = {
        "reference_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "scope": data["scope"],
        "current": evaluate(rank_news, data),
    }
    if args.baseline_commit:
        code = subprocess.run(
            ["git", "show", args.baseline_commit + ":src/alfa_agent/news_ranking.py"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        namespace = {"__package__": "alfa_agent", "__name__": "alfa_agent.ranking_baseline"}
        exec(compile(code, "ranking_baseline", "exec"), namespace)
        result["baseline"] = {"commit": args.baseline_commit, **evaluate(namespace["rank_news"], data)}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: {
                    x: v[x]
                    for x in [
                        "mean_precision_at_5",
                        "minimum_precision_at_5",
                        "minimum_critical_recall_at_20",
                    ]
                }
                for k, v in result.items()
                if k in {"current", "baseline"}
            }
        )
    )
    for case in result["current"]["cases"]:
        if case["precision_at_5"] < 0.8 or case["missed_critical"]:
            print(json.dumps(case, ensure_ascii=False))
    if (
        result["current"]["minimum_precision_at_5"] < 0.8
        or result["current"]["minimum_critical_recall_at_20"] < 1
    ):
        raise SystemExit("Editorial reference gate failed")


if __name__ == "__main__":
    main()
