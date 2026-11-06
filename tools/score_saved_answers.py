"""Score saved AgentOS answers using IndustryBench's released judge prompts."""
import argparse
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def curl_completion(config, messages, max_tokens):
    key = config.get("api_key") or os.environ.get(config.get("api_key_env", ""), "")
    base = config.get("base_url", "").rstrip("/")
    model = config.get("model", "")
    if not key or not base or not model:
        raise ValueError("请在裁判配置文件中填写 base_url、model 和 api_key（或 api_key_env）")
    if any(c in key for c in '\r\n"\\'):
        raise ValueError("API Key 含非法控制字符或引号")
    payload = {"model": model, "messages": messages, "temperature": 0,
               "max_tokens": max_tokens, "stream": False}
    payload.update(config.get("extra_body", {}))
    # Credentials go to curl via stdin, never in the process command line or logs.
    curl_config = f'header = "Authorization: Bearer {key}"\n'
    # Keep long knowledge passages out of the Windows command line. The temporary
    # request body contains benchmark text only; credentials remain on curl stdin.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False, dir=os.environ["AIMS_REQUEST_TEMP"]) as body_file:
        body_file.write(json.dumps(payload, ensure_ascii=True))
        body_path = body_file.name
    try:
        proc = subprocess.run(
            ["curl.exe" if os.name == "nt" else "curl", "--silent", "--show-error",
             "--max-time", "180", "--config", "-", "--header", "Content-Type: application/json",
             "--data-binary", "@" + body_path,
             "--write-out", "\n%{http_code}", base + "/chat/completions"],
            input=curl_config.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=190,
        )
    finally:
        Path(body_path).unlink(missing_ok=True)
    if proc.returncode:
        return {"success": False, "error": f"curl transport error {proc.returncode}"}
    raw, _, status = proc.stdout.rpartition(b"\n")
    if status != b"200":
        return {"success": False, "error": f"HTTP {status.decode(errors='replace')}"}
    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
        return {"success": True, "content": content, "usage": data.get("usage"), "model": model}
    except (ValueError, KeyError, IndexError, TypeError):
        return {"success": False, "error": "invalid Chat Completions response"}


def parse_json_content(content):
    value = content.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0]
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("not object")
    return result


def judge_callback(config, field):
    def call(messages, max_tokens=500, **kwargs):
        result = curl_completion(config, messages, max_tokens)
        if not result["success"]:
            return result
        try:
            parsed = parse_json_content(result["content"])
            value = parsed[field]
            if field == "score":
                if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) not in (0, 1, 2, 3):
                    raise ValueError("score outside rubric")
            elif type(value) is not bool:
                raise ValueError("safety_violation must be boolean")
            if not isinstance(parsed.get("reason"), str):
                raise ValueError("reason missing")
            # Canonical JSON also avoids the upstream permissive fence parser.
            result["content"] = json.dumps(parsed, ensure_ascii=False)
            return result
        except (ValueError, KeyError, TypeError, OverflowError):
            return {"success": False, "error": f"invalid judge JSON field: {field}"}
    return call


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / ".agentos-benchmark/judge.json")
    parser.add_argument("--check", action="store_true", help="One small API connectivity request, no scoring")
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if args.check:
        result = curl_completion(config, [{"role": "user", "content": 'Return only {"ok":true}'}], 64)
        print(json.dumps({"success": result["success"], "model": config.get("model"),
                          "error": result.get("error")}, ensure_ascii=False))
        return 0 if result["success"] else 1
    samples = {r["id"]: r for r in json.loads(args.samples.read_text(encoding="utf-8"))}
    answers = json.loads(args.answers.read_text(encoding="utf-8"))
    ids = [r["id"] for r in answers]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate answer IDs")
    sys.path.insert(0, str(ROOT / "third_party" / "IndustryBench"))
    upstream = importlib.import_module("evaluate")
    judge = upstream.LLMJudge(config["model"], judge_callback(config, "score"), api_key="configured-locally")
    reviewer = upstream.SafetyReviewer(config["model"], judge_callback(config, "safety_violation"), api_key="configured-locally")
    result_rows = []
    for answer in answers:
        sample = samples[answer["id"]]
        result = {**answer, "judge_model": config["model"], "score": None, "final_score": None,
                  "safety_violation": None, "evaluation_status": "not_answered"}
        text = answer.get("model_response", "")
        if answer.get("status") == "answered" and text.strip():
            score = judge.evaluate(sample["question"], sample["answer"], text)
            result["judge"] = score
            result["evaluation_status"] = "judge_failed"
            if score["success"]:
                result["score"] = score["score"]
                safety = reviewer.review(sample["question"], sample["answer"], sample["knowledge_text"], text)
                result["safety_review"] = safety
                result["evaluation_status"] = "safety_review_failed"
                if safety["success"]:
                    result["safety_violation"] = safety["safety_violation"]
                    result["final_score"] = 0 if safety["safety_violation"] else score["score"]
                    result["evaluation_status"] = "scored"
        result_rows.append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"id={answer['id']}: {result['evaluation_status']}", flush=True)
    scored = [r for r in result_rows if r["evaluation_status"] == "scored"]
    print(json.dumps({"answered": len(answers), "fully_scored": len(scored),
                      "mean_final_score": sum(r["final_score"] for r in scored) / len(scored) if scored else None,
                      "scope": "smoke_sample_only"}, ensure_ascii=False))
    return 0 if len(scored) == len(answers) and answers else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        # Never print exception contents that may include provider responses or secrets.
        print(f"Configuration or transport failure: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(2)
