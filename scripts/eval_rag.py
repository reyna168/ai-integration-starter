"""Run the golden RAG set and report retrieval hit rate and keyword answer accuracy."""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import db, rag  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", default=str(ROOT / "data" / "golden_questions.json"))
    parser.add_argument("--directory", default=str(ROOT / "docs"))
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    db.init_db()
    if not args.skip_ingest:
        print(f"Indexed/updated chunks: {rag.ingest_dir(args.directory)}")
    questions = json.loads(pathlib.Path(args.questions).read_text(encoding="utf-8"))
    results = []
    for item in questions:
        retrieved = rag.retrieve(item["question"])
        expected = set(item.get("expected_sources", []))
        sources = {hit["source"] for hit in retrieved}
        response = rag.answer(item["question"])
        text = response["answer"].casefold()
        required = item.get("must_contain", [])
        correct = all(term.casefold() in text for term in required)
        results.append({"question": item["question"],
                        "retrieval_hit": bool(expected & sources) if expected else bool(retrieved),
                        "answer_correct": correct,
                        "matched_sources": sorted(sources & expected),
                        "missing_terms": [term for term in required if term.casefold() not in text],
                        "answer": response["answer"],
                        "sources": response["sources"]})
        print(f"{'HIT' if results[-1]['retrieval_hit'] else 'MISS'} | "
              f"{'PASS' if correct else 'FAIL'} | {item['question']}")

    count = max(1, len(results))
    summary = {
        "questions": len(results),
        "retrieval_hit_rate": round(sum(x["retrieval_hit"] for x in results) / count, 4),
        "answer_correctness": round(sum(x["answer_correct"] for x in results) / count, 4),
        "results": results,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2))
    if args.output:
        pathlib.Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
        print(f"Detailed results saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
