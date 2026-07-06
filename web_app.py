#!/usr/bin/env python3
"""
Mobile-first Web/PWA entry point for the graduation review system.

The app follows the interaction architecture in the supplied HTML diagram:
upload -> task id -> SSE progress -> result/report/history/stats.
It intentionally uses the Python standard library so it can run before a
frontend build pipeline or web framework is introduced.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import html
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
import warnings
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore", message="'cgi' is deprecated.*", category=DeprecationWarning)
import cgi


PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
REPORT_DIR = RUNTIME_DIR / "reports"
WORK_DIR = RUNTIME_DIR / "work"
DB_PATH = RUNTIME_DIR / "review_tasks.sqlite3"
ALLOWED_EXTENSIONS = {".doc", ".docx", ".pdf", ".pptx", ".zip"}
REVIEW_FILE_EXTENSIONS = {".doc", ".docx", ".pdf", ".pptx"}
DEFAULT_FORMATS = ["md", "json", "html"]
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
STEP_NAMES = [
    "文档提取",
    "单文档评分",
    "跨文档交叉审查",
    "GB/T 7714 校验",
    "格式规范检查",
    "PPT 分析",
    "安全护栏",
    "报告生成",
]

sys.path.insert(0, str(PROJECT_ROOT))
import main as review_main  # noqa: E402


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        exc_type, _, _ = sys.exc_info()
        if exc_type in {BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError}:
            return
        super().handle_error(request, client_address)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_runtime_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_runtime_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                archive_dir TEXT NOT NULL,
                upload_dir TEXT,
                report_dir TEXT NOT NULL,
                formats_json TEXT NOT NULL,
                files_json TEXT NOT NULL,
                result_json TEXT,
                events_json TEXT NOT NULL,
                logs TEXT NOT NULL DEFAULT '',
                error TEXT,
                score REAL,
                grade TEXT,
                issue_count INTEGER NOT NULL DEFAULT 0,
                file_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.commit()


def row_to_task(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    task = dict(row)
    for key in ("formats_json", "files_json", "events_json", "result_json"):
        raw = task.get(key)
        if raw:
            task[key.removesuffix("_json")] = json.loads(raw)
        else:
            task[key.removesuffix("_json")] = [] if key != "result_json" else None
    return task


def get_task(task_id: str) -> dict | None:
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row_to_task(row)


def create_task(archive_dir: Path, mode: str, formats: list[str], files: list[str], upload_dir: Path | None) -> dict:
    task_id = uuid.uuid4().hex[:12]
    report_dir = REPORT_DIR / task_id
    report_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "id": task_id,
        "status": "queued",
        "mode": mode,
        "archive_dir": str(archive_dir),
        "upload_dir": str(upload_dir) if upload_dir else None,
        "report_dir": str(report_dir),
        "formats": formats,
        "files": files,
        "events": [
            {
                "index": 0,
                "step": "任务创建",
                "status": "done",
                "message": "审查任务已创建",
                "time": now_iso(),
            }
        ],
        "logs": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, status, mode, archive_dir, upload_dir, report_dir,
                formats_json, files_json, events_json, logs,
                file_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task["id"],
                task["status"],
                task["mode"],
                task["archive_dir"],
                task["upload_dir"],
                task["report_dir"],
                json.dumps(task["formats"], ensure_ascii=False),
                json.dumps(task["files"], ensure_ascii=False),
                json.dumps(task["events"], ensure_ascii=False),
                "",
                len(files),
                task["created_at"],
                task["updated_at"],
            ),
        )
        conn.commit()
    return task


def update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [task_id]
    with connect_db() as conn:
        conn.execute(f"UPDATE tasks SET {columns} WHERE id = ?", values)
        conn.commit()


def append_event(task_id: str, index: int, step: str, status: str, message: str) -> None:
    task = get_task(task_id)
    if not task:
        return
    events = task.get("events", [])
    events.append(
        {
            "index": index,
            "step": step,
            "status": status,
            "message": message,
            "time": now_iso(),
        }
    )
    update_task(task_id, status="running" if status == "running" else task["status"], events_json=json.dumps(events, ensure_ascii=False))


def finish_task(task_id: str, result: dict, logs: str) -> None:
    summary = summarize_result(result)
    task = get_task(task_id)
    events = task.get("events", []) if task else []
    events.append(
        {
            "index": 9,
            "step": "完成",
            "status": "done",
            "message": "审查完成",
            "time": now_iso(),
        }
    )
    update_task(
        task_id,
        status="done",
        result_json=json.dumps(result, ensure_ascii=False, default=str),
        events_json=json.dumps(events, ensure_ascii=False),
        logs=logs,
        score=summary["score"],
        grade=summary["grade"],
        issue_count=summary["issue_count"],
        file_count=summary["file_count"],
        finished_at=now_iso(),
    )


def fail_task(task_id: str, error: str, logs: str) -> None:
    task = get_task(task_id)
    events = task.get("events", []) if task else []
    events.append(
        {
            "index": 99,
            "step": "异常",
            "status": "error",
            "message": error,
            "time": now_iso(),
        }
    )
    update_task(
        task_id,
        status="failed",
        events_json=json.dumps(events, ensure_ascii=False),
        error=error,
        logs=logs,
        finished_at=now_iso(),
    )


def safe_filename(name: str) -> str:
    clean = os.path.basename(name).replace("\x00", "").strip()
    if not clean:
        clean = f"upload-{uuid.uuid4().hex}"
    return clean


def safe_extract_zip(zip_path: Path, target_dir: Path) -> list[str]:
    extracted = []
    unpack_dir = target_dir / f"{zip_path.stem}_unzipped"
    unpack_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member_name = info.filename.replace("\\", "/").lstrip("/")
            if ".." in Path(member_name).parts:
                continue
            filename = safe_filename(Path(member_name).name)
            ext = Path(filename).suffix.lower()
            if ext not in REVIEW_FILE_EXTENSIONS:
                continue
            target = (unpack_dir / filename).resolve()
            if not str(target).startswith(str(unpack_dir.resolve())):
                continue
            with archive.open(info) as src, target.open("wb") as out:
                out.write(src.read())
            extracted.append(str(target.relative_to(target_dir)))
    return extracted


def save_review_upload(upload_dir: Path, filename: str, content: bytes) -> list[str]:
    filename = safe_filename(filename)
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return []
    target = upload_dir / filename
    target.write_bytes(content)
    if ext == ".zip":
        return safe_extract_zip(target, upload_dir)
    return [filename]


def normalize_formats(value: str | None) -> list[str]:
    if not value:
        return DEFAULT_FORMATS[:]
    formats = [item.strip().lower() for item in value.split(",") if item.strip()]
    return formats or DEFAULT_FORMATS[:]


def dependency_status() -> dict:
    modules = {
        "docx": "python-docx",
        "pptx": "python-pptx",
        "pypdf": "pypdf",
        "yaml": "PyYAML",
        "olefile": "olefile",
        "Crypto": "pycryptodome",
    }
    status = {}
    for module, package in modules.items():
        try:
            __import__(module)
            status[package] = True
        except Exception:
            status[package] = False
    return status


def is_llm_configured() -> bool:
    return bool(review_main.CONFIG.get("llm", {}).get("api_key"))


def validate_review_mode(mode: str) -> str | None:
    if mode not in {"code", "skill", "both"}:
        return "审查模式无效"
    if mode in {"skill", "both"} and not is_llm_configured():
        return "LLM API Key 未配置，请先配置 LLM_API_KEY，或选择“规则”模式"
    return None


def is_local_client(handler: BaseHTTPRequestHandler) -> bool:
    host = handler.client_address[0] if handler.client_address else ""
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("::ffff:127.")


def report_content_type(path: Path) -> str:
    guessed = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if guessed.startswith("text/") or guessed in {"application/json", "application/javascript"}:
        return f"{guessed}; charset=utf-8"
    return guessed


def reviewable_files(directory: Path) -> list[str]:
    return [
        item.name
        for item in sorted(directory.iterdir(), key=lambda p: p.name.lower())
        if item.is_file() and not item.name.startswith("~$") and item.suffix.lower() in REVIEW_FILE_EXTENSIONS
    ]


def normalize_issue(issue: dict) -> dict:
    return {
        "file": issue.get("file") or issue.get("filename") or issue.get("document") or "-",
        "category": issue.get("category") or issue.get("type") or issue.get("dimension") or issue.get("check") or "-",
        "severity": issue.get("severity") or issue.get("level") or issue.get("confidence") or "-",
        "message": issue.get("issue") or issue.get("message") or issue.get("description") or str(issue),
        "evidence": issue.get("evidence") or issue.get("snippet") or "",
    }


def aggregate_number_duplicate_issues(issues: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str, str], dict] = {}
    output = []
    pattern = re.compile(r"^(图|表)(\d+)-(\d+)\s+出现重复$")

    for item in issues:
        message = item["message"].strip()
        match = pattern.match(message)
        if not match:
            output.append(item)
            continue

        label, chapter, number = match.groups()
        key = (item["file"], label, chapter)
        group = groups.setdefault(
            key,
            {
                "file": item["file"],
                "category": f"{label}编号重复",
                "severity": item["severity"],
                "label": label,
                "chapter": chapter,
                "numbers": set(),
            },
        )
        group["numbers"].add(int(number))

    for group in groups.values():
        numbers = sorted(group["numbers"])
        shown = [f'{group["label"]}{group["chapter"]}-{number}' for number in numbers[:12]]
        suffix = f" 等，共{len(numbers)}处" if len(numbers) > 12 else f"，共{len(numbers)}处"
        output.append(
            {
                "file": group["file"],
                "category": group["category"],
                "severity": group["severity"],
                "message": f'第{group["chapter"]}章{group["label"]}编号疑似重复：{", ".join(shown)}{suffix}',
                "evidence": "",
            }
        )

    return output


def collect_issues(result: dict) -> list[dict]:
    issues = []
    for key in ("clean_issues", "issues", "gbt7714_issues", "format_issues"):
        value = result.get(key, [])
        if isinstance(value, list):
            issues.extend(item for item in value if isinstance(item, dict))
    seen = set()
    normalized = []
    for issue in issues:
        item = normalize_issue(issue)
        marker = issue_dedupe_key(issue, item)
        if marker not in seen:
            seen.add(marker)
            normalized.append(item)
    return aggregate_number_duplicate_issues(normalized)


def issue_dedupe_key(raw_issue: dict, item: dict) -> str:
    message = item["message"]
    rule_id = raw_issue.get("rule_id", "")

    # Reference checks can repeat because PDF extraction may duplicate headers,
    # footers, reference fragments, or page-local citation snippets. For these,
    # dedupe by rule and normalized message instead of evidence text.
    if rule_id or message.startswith("[#"):
        message = re.sub(r"^\[#\d+\]", "[#]", message)
        message = re.sub(r"\s+", " ", message).strip()
        return json.dumps(
            {
                "file": item["file"],
                "rule_id": rule_id,
                "message": message,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    return json.dumps(
        {
            "file": item["file"],
            "category": item["category"],
            "message": message,
            "evidence": item["evidence"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def extract_score(result: dict) -> float | None:
    candidates = [
        result.get("final_score"),
        result.get("score"),
        result.get("total_score"),
    ]
    single = result.get("single_doc_scores")
    if isinstance(single, dict):
        candidates.extend([single.get("final_score"), single.get("score"), single.get("total_score")])
        doc_scores = []
        for value in single.values():
            if isinstance(value, dict):
                for key in ("总分", "total", "score", "total_score"):
                    raw = value.get(key)
                    if isinstance(raw, (int, float)):
                        doc_scores.append(float(raw))
        if doc_scores:
            candidates.append(sum(doc_scores) / len(doc_scores))
    for candidate in candidates:
        if isinstance(candidate, (int, float)):
            return round(float(candidate), 1)
    return None


def grade_from_score(score: float | None) -> str:
    if score is None:
        return "待评估"
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "良好"
    if score >= 60:
        return "合格"
    return "需整改"


def summarize_result(result: dict) -> dict:
    issues = collect_issues(result)
    score = extract_score(result)
    grade = result.get("grade") or grade_from_score(score)
    manifest = result.get("manifest", [])
    return {
        "score": score,
        "grade": grade,
        "issue_count": len(issues),
        "file_count": len(manifest) if isinstance(manifest, list) else 0,
        "issues": issues,
        "reports": result.get("report_paths", []),
    }


def render_html_report(task: dict, result: dict) -> Path:
    summary = summarize_result(result)
    report_dir = Path(task["report_dir"])
    report_path = report_dir / "mobile_review_report.html"
    issue_rows = "\n".join(
        f"<tr><td>{html.escape(item['file'])}</td><td>{html.escape(item['category'])}</td>"
        f"<td>{html.escape(str(item['severity']))}</td><td>{html.escape(item['message'])}</td></tr>"
        for item in summary["issues"][:200]
    )
    if not issue_rows:
        issue_rows = '<tr><td colspan="4">暂无问题</td></tr>'
    report_path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>审查报告</title>
<style>
body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:0;background:#f5f7fb;color:#111827}}
main{{max-width:980px;margin:0 auto;padding:24px}}
.hero{{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin-bottom:16px}}
h1{{font-size:22px;margin:0 0 10px}} .score{{font-size:42px;font-weight:700;color:#2563eb}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb}}
th,td{{padding:10px;border-bottom:1px solid #e5e7eb;text-align:left;font-size:14px;vertical-align:top}}
th{{background:#f9fafb}}
</style>
<main>
<section class="hero">
<h1>毕业设计智能审查报告</h1>
<div>任务编号：{html.escape(task['id'])}</div>
<div>审查模式：{html.escape(task['mode'])}</div>
<div class="score">{summary['score'] if summary['score'] is not None else '--'}</div>
<div>等级：{html.escape(summary['grade'])}，问题数：{summary['issue_count']}</div>
</section>
<table>
<thead><tr><th>文件</th><th>类型</th><th>级别</th><th>问题说明</th></tr></thead>
<tbody>{issue_rows}</tbody>
</table>
</main>
</html>""",
        encoding="utf-8",
    )
    return report_path


def run_pipeline_with_progress(task_id: str) -> None:
    task = get_task(task_id)
    if not task:
        return
    buffer = io.StringIO()
    archive_dir = task["archive_dir"]
    report_dir = task["report_dir"]
    mode = task["mode"]
    formats = task["formats"]
    task_work_dir = WORK_DIR / task_id
    task_work_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "student_info": {"archive_dir": archive_dir, "name": Path(archive_dir).name},
        "review_date": now_iso(),
        "mode": mode,
        "work_dir": str(task_work_dir),
        "single_doc_scores": {},
        "cross_check": {},
        "gbt7714_issues": [],
        "format_issues": [],
        "ppt_review": {},
        "issues": [],
        "clean_issues": [],
        "harness_report": {},
        "report_paths": [],
    }
    started = time.time()
    try:
        update_task(task_id, status="running")
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            append_event(task_id, 1, STEP_NAMES[0], "running", "正在解析上传材料")
            manifest = review_main.step_extract(archive_dir, temp_dir=str(task_work_dir))
            result["manifest"] = manifest
            append_event(task_id, 1, STEP_NAMES[0], "done", f"完成，共解析 {len(manifest)} 个文件")

            if not manifest:
                result["issues"].append(
                    {
                        "file": "-",
                        "category": "上传材料",
                        "severity": "high",
                        "issue": "未找到可解析的文档文件",
                        "evidence": archive_dir,
                    }
                )
            else:
                append_event(task_id, 2, STEP_NAMES[1], "running", "正在计算单文档评分")
                scores = review_main.step_scoring(manifest, mode)
                result["single_doc_scores"] = scores.get("single_doc_scores", {}) if isinstance(scores, dict) else {}
                scoring_issues = scores.get("issues", []) if isinstance(scores, dict) else []
                result["issues"].extend(scoring_issues)
                append_event(task_id, 2, STEP_NAMES[1], "done", "单文档评分完成")

                append_event(task_id, 3, STEP_NAMES[2], "running", "正在检查跨文档一致性")
                result["cross_check"] = review_main.step_cross_check(manifest, mode)
                append_event(task_id, 3, STEP_NAMES[2], "done", "跨文档一致性检查完成")

                append_event(task_id, 4, STEP_NAMES[3], "running", "正在校验参考文献格式")
                result["gbt7714_issues"] = review_main.step_gbt7714(manifest, mode)
                append_event(task_id, 4, STEP_NAMES[3], "done", f"发现 {len(result['gbt7714_issues'])} 条参考文献问题")

                append_event(task_id, 5, STEP_NAMES[4], "running", "正在检查格式规范")
                result["format_issues"] = review_main.step_format_check(manifest, mode)
                append_event(task_id, 5, STEP_NAMES[4], "done", f"发现 {len(result['format_issues'])} 条格式问题")

                append_event(task_id, 6, STEP_NAMES[5], "running", "正在分析答辩 PPT")
                ppt = review_main.step_ppt_analysis(manifest, mode)
                result["ppt_review"] = ppt.get("ppt_review", {}) if isinstance(ppt, dict) else {}
                append_event(task_id, 6, STEP_NAMES[5], "done", "PPT 分析完成")

                all_issues = result["issues"] + result["gbt7714_issues"] + result["format_issues"]
                result["issues"] = all_issues

                append_event(task_id, 7, STEP_NAMES[6], "running", "正在运行安全护栏")
                harness_result = review_main.step_harness(all_issues, archive_dir, temp_dir=str(task_work_dir))
                result["harness_report"] = harness_result
                result["clean_issues"] = harness_result.get("clean_issues", all_issues) if isinstance(harness_result, dict) else all_issues
                append_event(task_id, 7, STEP_NAMES[6], "done", "安全护栏完成")

            append_event(task_id, 8, STEP_NAMES[7], "running", "正在生成报告")
            result["report_paths"] = review_main.step_report(result, report_dir, formats)
            report_path = render_html_report(task, result)
            result["report_paths"].append(str(report_path))
            result["elapsed_seconds"] = round(time.time() - started, 1)
            append_event(task_id, 8, STEP_NAMES[7], "done", "报告生成完成")
        finish_task(task_id, result, buffer.getvalue())
    except Exception as exc:
        fail_task(task_id, str(exc), buffer.getvalue() + "\n" + traceback.format_exc())


def start_task_worker(task_id: str) -> None:
    thread = threading.Thread(target=run_pipeline_with_progress, args=(task_id,), daemon=True)
    thread.start()


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "GraduationReviewPWA/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[web] {self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            text_response(self, HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/health":
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "app": "毕业设计智能审查系统",
                    "version": "1.0",
                    "llm_configured": is_llm_configured(),
                    "dependencies": dependency_status(),
                },
            )
        elif path.startswith("/api/review/status/"):
            self.handle_sse_status(path.rsplit("/", 1)[-1])
        elif path.startswith("/api/review/result/"):
            self.handle_result(path.rsplit("/", 1)[-1])
        elif path.startswith("/api/review/report/"):
            self.handle_report(path.rsplit("/", 1)[-1], parse_qs(parsed.query).get("fmt", ["html"])[0])
        elif path == "/api/history":
            self.handle_history()
        elif path == "/api/stats":
            self.handle_stats()
        elif path == "/manifest.webmanifest":
            text_response(self, HTTPStatus.OK, WEB_MANIFEST, "application/manifest+json; charset=utf-8")
        elif path == "/sw.js":
            text_response(self, HTTPStatus.OK, SERVICE_WORKER, "application/javascript; charset=utf-8")
        else:
            text_response(self, HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/review/upload":
            self.handle_upload()
        elif parsed.path == "/api/review/path":
            self.handle_path_review()
        else:
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length)
        return json.loads(data.decode("utf-8")) if data else {}

    def handle_upload(self) -> None:
        try:
            ensure_runtime_dirs()
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > MAX_UPLOAD_BYTES:
                json_response(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "上传文件过大"})
                return
            upload_dir = UPLOAD_DIR / uuid.uuid4().hex[:12]
            upload_dir.mkdir(parents=True, exist_ok=True)
            content_type = self.headers.get("Content-Type", "")
            if content_type.startswith("application/json"):
                payload = self.read_json_body()
                mode = payload.get("mode", "code")
                formats = normalize_formats(payload.get("formats", ",".join(DEFAULT_FORMATS)))
                mode_error = validate_review_mode(mode)
                if mode_error:
                    json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": mode_error})
                    return
                saved = []
                for item in payload.get("files", []):
                    filename = item.get("name", "")
                    data = item.get("data", "")
                    if not filename or not data:
                        continue
                    try:
                        content = base64.b64decode(data, validate=True)
                    except Exception:
                        json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"文件编码无效: {filename}"})
                        return
                    saved.extend(save_review_upload(upload_dir, filename, content))
                if not saved:
                    json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "未收到可审查文件"})
                    return
                task = create_task(upload_dir, mode, formats, saved, upload_dir)
                start_task_worker(task["id"])
                json_response(self, HTTPStatus.ACCEPTED, {"ok": True, "task_id": task["id"], "status": "queued", "files": saved})
                return

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )
            mode = form.getfirst("mode", "code")
            formats = normalize_formats(form.getfirst("formats", ",".join(DEFAULT_FORMATS)))
            mode_error = validate_review_mode(mode)
            if mode_error:
                json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": mode_error})
                return
            fields = form["files"] if "files" in form else []
            if not isinstance(fields, list):
                fields = [fields]
            saved = []
            for field in fields:
                if not getattr(field, "filename", ""):
                    continue
                filename = safe_filename(field.filename)
                ext = Path(filename).suffix.lower()
                if ext not in ALLOWED_EXTENSIONS:
                    continue
                saved.extend(save_review_upload(upload_dir, filename, field.file.read()))
            if not saved:
                json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "未收到可审查文件"})
                return
            task = create_task(upload_dir, mode, formats, saved, upload_dir)
            start_task_worker(task["id"])
            json_response(self, HTTPStatus.ACCEPTED, {"ok": True, "task_id": task["id"], "status": "queued", "files": saved})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def handle_path_review(self) -> None:
        try:
            payload = self.read_json_body()
            archive_dir = Path(payload.get("archive_dir", "")).expanduser().resolve()
            mode = payload.get("mode", "code")
            formats = normalize_formats(payload.get("formats", ",".join(DEFAULT_FORMATS)))
            mode_error = validate_review_mode(mode)
            if mode_error:
                json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": mode_error})
                return
            if not is_local_client(self) and os.environ.get("ALLOW_REMOTE_PATH_REVIEW") != "1":
                json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "目录审查仅限服务器本机访问，手机端请使用上传审查"})
                return
            if not archive_dir.exists() or not archive_dir.is_dir():
                json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "目录不存在"})
                return
            files = reviewable_files(archive_dir)
            if not files:
                json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "目录中没有可审查的 DOC/DOCX/PDF/PPTX 文件"})
                return
            task = create_task(archive_dir, mode, formats, files, None)
            start_task_worker(task["id"])
            json_response(self, HTTPStatus.ACCEPTED, {"ok": True, "task_id": task["id"], "status": "queued", "files": files})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def handle_sse_status(self, task_id: str) -> None:
        task = get_task(task_id)
        if not task:
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sent = 0
        for _ in range(600):
            task = get_task(task_id)
            if not task:
                break
            events = task.get("events", [])
            while sent < len(events):
                payload = json.dumps({"task": task_summary(task), "event": events[sent]}, ensure_ascii=False)
                if not self.write_sse_payload(payload):
                    return
                sent += 1
            if task["status"] in {"done", "failed"}:
                final_payload = json.dumps({"task": task_summary(task), "event": {"status": task["status"], "step": "finished"}}, ensure_ascii=False)
                self.write_sse_payload(final_payload)
                break
            time.sleep(1)

    def write_sse_payload(self, payload: str) -> bool:
        try:
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def handle_result(self, task_id: str) -> None:
        task = get_task(task_id)
        if not task:
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在"})
            return
        payload = {"ok": True, "task": task_summary(task), "events": task.get("events", []), "logs": task.get("logs", "")}
        if task.get("result"):
            payload["result"] = task["result"]
            payload["summary"] = summarize_result(task["result"])
        if task.get("error"):
            payload["error"] = task["error"]
        json_response(self, HTTPStatus.OK, payload)

    def handle_report(self, task_id: str, fmt: str) -> None:
        task = get_task(task_id)
        if not task:
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在"})
            return
        report_dir = Path(task["report_dir"])
        candidates = sorted(report_dir.glob(f"*.{fmt}"))
        if fmt == "html":
            preferred = report_dir / "mobile_review_report.html"
            if preferred.exists():
                candidates = [preferred]
        if not candidates:
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": f"暂无 {fmt} 报告"})
            return
        target = candidates[0]
        content_type = report_content_type(target)
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{target.name}"')
        self.end_headers()
        self.wfile.write(data)

    def handle_history(self) -> None:
        with connect_db() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 50").fetchall()
        json_response(self, HTTPStatus.OK, {"ok": True, "items": [task_summary(row_to_task(row)) for row in rows]})

    def handle_stats(self) -> None:
        with connect_db() as conn:
            rows = conn.execute("SELECT status, score, grade, issue_count FROM tasks").fetchall()
        total = len(rows)
        done = [row for row in rows if row["status"] == "done"]
        scores = [float(row["score"]) for row in done if row["score"] is not None]
        grade_dist = {}
        for row in done:
            grade = row["grade"] or "待评估"
            grade_dist[grade] = grade_dist.get(grade, 0) + 1
        json_response(
            self,
            HTTPStatus.OK,
            {
                "ok": True,
                "total": total,
                "done": len(done),
                "failed": sum(1 for row in rows if row["status"] == "failed"),
                "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
                "issue_total": sum(int(row["issue_count"] or 0) for row in done),
                "grade_dist": grade_dist,
            },
        )


def task_summary(task: dict) -> dict:
    return {
        "id": task["id"],
        "status": task["status"],
        "mode": task["mode"],
        "score": task.get("score"),
        "grade": task.get("grade"),
        "issue_count": task.get("issue_count", 0),
        "file_count": task.get("file_count", 0),
        "files": task.get("files", []),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "finished_at": task.get("finished_at"),
        "error": task.get("error"),
    }


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#2563eb">
  <link rel="manifest" href="/manifest.webmanifest">
  <title>毕设智能审查</title>
  <style>
    :root {
      --primary:#2563eb; --primary-soft:#dbeafe; --success:#16a34a;
      --warning:#f59e0b; --danger:#dc2626; --bg:#f1f5f9; --panel:#fff;
      --line:#e5e7eb; --muted:#6b7280; --ink:#111827; --radius:12px;
    }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:"Microsoft YaHei",Arial,sans-serif; letter-spacing:0; }
    .app { min-height:100vh; max-width:520px; margin:0 auto; background:#fff; display:flex; flex-direction:column; box-shadow:0 0 0 1px rgba(15,23,42,.04); }
    header { padding:18px 18px 12px; background:#fff; position:sticky; top:0; z-index:3; border-bottom:1px solid var(--line); }
    .topline { display:flex; justify-content:space-between; align-items:center; gap:12px; }
    h1 { margin:0; font-size:20px; }
    .sub { margin-top:5px; color:var(--muted); font-size:12px; }
    main { flex:1; padding:16px; padding-bottom:84px; }
    .page { display:none; }
    .page.active { display:block; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius); padding:14px; margin-bottom:12px; }
    .upload-zone { border:2px dashed #cbd5e1; border-radius:var(--radius); padding:28px 14px; text-align:center; background:#f8fafc; margin-bottom:12px; }
    .upload-zone strong { display:block; font-size:15px; margin:8px 0 4px; }
    input[type=file], input[type=text], select { width:100%; min-height:42px; border:1px solid var(--line); border-radius:8px; padding:9px 10px; font:inherit; background:#fff; }
    label { display:block; font-size:12px; color:var(--muted); margin:10px 0 6px; }
    .segmented { display:grid; grid-template-columns:repeat(3,1fr); border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-bottom:10px; }
    .segmented button { border:0; border-right:1px solid var(--line); background:#fff; min-height:38px; font:inherit; }
    .segmented button:last-child { border-right:0; }
    .segmented button.active { background:var(--primary); color:#fff; font-weight:700; }
    .segmented button:disabled { color:#9ca3af; background:#f9fafb; cursor:not-allowed; }
    .btn { width:100%; min-height:44px; border:0; border-radius:8px; font:inherit; font-weight:700; cursor:pointer; }
    .btn.primary { background:var(--primary); color:#fff; }
    .btn.ghost { background:#fff; color:var(--primary); border:1px solid var(--primary); }
    .row { display:flex; justify-content:space-between; align-items:center; gap:10px; }
    .badge { display:inline-flex; align-items:center; min-height:24px; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:700; }
    .ok { background:#dcfce7; color:#166534; } .warn { background:#fef3c7; color:#92400e; } .bad { background:#fee2e2; color:#991b1b; }
    .muted { color:var(--muted); font-size:12px; }
    .progress-bar { height:8px; border-radius:8px; background:#e5e7eb; overflow:hidden; margin-top:10px; }
    .progress-fill { height:100%; width:0%; background:var(--primary); transition:width .25s ease; }
    .step { display:flex; align-items:flex-start; gap:10px; padding:10px 0; border-bottom:1px solid #f1f5f9; font-size:13px; }
    .step:last-child { border-bottom:0; }
    .dot { width:10px; height:10px; border-radius:50%; margin-top:4px; background:#cbd5e1; flex:none; }
    .dot.running { background:var(--primary); box-shadow:0 0 0 5px var(--primary-soft); }
    .dot.done { background:var(--success); }
    .dot.error { background:var(--danger); }
    .score-wrap { text-align:center; padding:12px 0 18px; }
    .score { width:112px; height:112px; border-radius:50%; display:grid; place-items:center; margin:0 auto 10px; background:conic-gradient(var(--success) 0deg, var(--success) 300deg, #e5e7eb 300deg); }
    .score-inner { width:82px; height:82px; border-radius:50%; background:#fff; display:grid; place-items:center; font-size:28px; font-weight:800; color:var(--success); }
    .metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
    .metric { background:#f8fafc; border:1px solid var(--line); border-radius:10px; padding:10px; text-align:center; }
    .metric b { display:block; font-size:18px; }
    .issue { border-left:4px solid var(--warning); }
    .issue.high { border-left-color:var(--danger); }
    .issue.low { border-left-color:#94a3b8; }
    .tabs { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:12px; }
    .tabs button { min-height:36px; border:1px solid var(--line); border-radius:8px; background:#fff; font:inherit; }
    .tabs button.active { border-color:var(--primary); color:var(--primary); font-weight:700; background:var(--primary-soft); }
    pre { white-space:pre-wrap; word-break:break-word; background:#0f172a; color:#e2e8f0; border-radius:10px; padding:12px; max-height:280px; overflow:auto; font-size:12px; }
    nav { position:fixed; bottom:0; left:50%; transform:translateX(-50%); width:min(520px,100%); background:#fff; border-top:1px solid var(--line); display:grid; grid-template-columns:repeat(4,1fr); padding:7px 0 calc(7px + env(safe-area-inset-bottom)); z-index:4; }
    nav button { border:0; background:#fff; color:var(--muted); font-size:11px; display:grid; gap:2px; place-items:center; }
    nav button span { font-size:20px; }
    nav button.active { color:var(--primary); font-weight:700; }
    .desktop-note { display:none; }
    @media (min-width:900px) {
      body { padding:24px; }
      .app { border-radius:24px; overflow:hidden; min-height:760px; }
      .desktop-note { display:block; max-width:860px; margin:0 auto 16px; color:#475569; text-align:center; }
    }
  </style>
</head>
<body>
<div class="desktop-note">移动端 Web/PWA 原型：用手机浏览器访问同一地址即可使用。</div>
<div class="app">
  <header>
    <div class="topline"><h1>毕设智能审查</h1><span class="badge ok" id="health">在线</span></div>
    <div class="sub">上传学生材料，自动完成 8 步审查并生成报告</div>
  </header>
  <main>
    <section class="page active" id="page-home">
      <div class="upload-zone">
        <div style="font-size:38px">📁</div>
        <strong>上传存档文件</strong>
        <div class="muted">支持 DOC/DOCX/PDF/PPTX/ZIP，手机端可多选文件</div>
      </div>
      <label>审查模式</label>
      <div class="segmented" id="modeTabs"><button data-mode="code" class="active">规则</button><button data-mode="skill">LLM</button><button data-mode="both">双跑</button></div>
      <label>选择文件</label><input type="file" id="files" multiple accept=".doc,.docx,.pdf,.pptx,.zip">
      <label>服务器本机材料文件夹路径（仅“目录审查”使用）</label><input type="text" id="archiveDir" placeholder="例如：E:\学生材料\张三">
      <label>报告格式</label>
      <select id="formats"><option value="md,json,html">Markdown + JSON + HTML</option><option value="md,json">Markdown + JSON</option><option value="json,html">JSON + HTML</option></select>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px">
        <button class="btn primary" id="uploadBtn">审查已选文件</button>
        <button class="btn ghost" id="pathBtn">审查目录</button>
      </div>
      <h3>最近审查</h3>
      <div id="recentList"><div class="card muted">暂无历史记录</div></div>
    </section>
    <section class="page" id="page-progress">
      <div class="card">
        <div class="row"><strong id="progressTitle">审查进度</strong><span id="progressText" class="muted">0/8</span></div>
        <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
      </div>
      <div class="card" id="stepList"></div>
    </section>
    <section class="page" id="page-results">
      <div class="score-wrap">
        <div class="score"><div class="score-inner" id="scoreValue">--</div></div>
        <div><span class="badge ok" id="gradeValue">待评估</span></div>
      </div>
      <div class="metrics">
        <div class="metric"><b id="fileCount">--</b><span class="muted">文件</span></div>
        <div class="metric"><b id="issueCount">--</b><span class="muted">问题</span></div>
        <div class="metric"><b id="modeValue">--</b><span class="muted">模式</span></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px">
        <button class="btn primary" id="htmlReportBtn">查看报告</button>
        <button class="btn ghost" id="issuesBtn">问题明细</button>
      </div>
      <div style="margin-top:10px">
        <button class="btn ghost" id="jsonReportBtn">JSON</button>
      </div>
    </section>
    <section class="page" id="page-issues">
      <div class="tabs"><button class="active" data-panel="issues">问题</button><button data-panel="events">进度</button><button data-panel="logs">日志</button></div>
      <div id="issuesPanel"></div>
      <div id="eventsPanel" style="display:none"></div>
      <pre id="logsPanel" style="display:none"></pre>
    </section>
    <section class="page" id="page-history">
      <div class="metrics" style="margin-bottom:12px">
        <div class="metric"><b id="statTotal">--</b><span class="muted">总数</span></div>
        <div class="metric"><b id="statAvg">--</b><span class="muted">均分</span></div>
        <div class="metric"><b id="statIssues">--</b><span class="muted">问题</span></div>
      </div>
      <div id="historyList"></div>
    </section>
  </main>
  <nav>
    <button class="active" data-page="home"><span>⌂</span>首页</button>
    <button data-page="progress"><span>↻</span>进度</button>
    <button data-page="results"><span>◉</span>报告</button>
    <button data-page="history"><span>≡</span>历史</button>
  </nav>
</div>
<script>
let currentMode = "code";
let currentTaskId = localStorage.getItem("currentTaskId") || "";
let lastResult = null;
let resultPollTimer = null;
const $ = (id) => document.getElementById(id);
const steps = ["文档提取","单文档评分","跨文档交叉审查","GB/T 7714 校验","格式规范检查","PPT 分析","安全护栏","报告生成"];

function showPage(name) {
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  $("page-" + name).classList.add("active");
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.page === name));
  if (name === "history") loadHistory();
}
document.querySelectorAll("nav button").forEach(b => b.onclick = () => showPage(b.dataset.page));
document.querySelectorAll("#modeTabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll("#modeTabs button").forEach(x => x.classList.remove("active"));
  b.classList.add("active"); currentMode = b.dataset.mode;
});
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  ["issues","events","logs"].forEach(name => $(name + "Panel").style.display = name === b.dataset.panel ? "" : "none");
});

function setSteps(events) {
  const done = new Set(events.filter(e => e.status === "done" && e.index >= 1 && e.index <= 8).map(e => e.index));
  const running = events.findLast ? events.findLast(e => e.status === "running") : events.filter(e => e.status === "running").slice(-1)[0];
  $("stepList").innerHTML = steps.map((name, i) => {
    const index = i + 1;
    const cls = done.has(index) ? "done" : (running && running.index === index ? "running" : "");
    const event = events.filter(e => e.index === index).slice(-1)[0];
    return `<div class="step"><div class="dot ${cls}"></div><div><b>${name}</b><div class="muted">${escapeHtml(event?.message || "等待中")}</div></div></div>`;
  }).join("");
  const count = done.size;
  $("progressText").textContent = count + "/8";
  $("progressFill").style.width = Math.min(100, count / 8 * 100) + "%";
  $("eventsPanel").innerHTML = events.slice().reverse().map(e => `<div class="card"><b>${escapeHtml(e.step)}</b><div class="muted">${escapeHtml(e.message || "")} · ${escapeHtml(e.time || "")}</div></div>`).join("") || `<div class="card muted">暂无进度</div>`;
}

function connectSse(taskId) {
  currentTaskId = taskId; localStorage.setItem("currentTaskId", taskId);
  showPage("progress");
  startResultPolling(taskId);
  const source = new EventSource(`/api/review/status/${taskId}`);
  source.onmessage = (message) => {
    const payload = JSON.parse(message.data);
    if (payload.task) $("progressTitle").textContent = `任务 ${payload.task.id}`;
    loadResult(taskId, false);
    if (payload.task && ["done","failed"].includes(payload.task.status)) {
      source.close();
      stopResultPolling();
      loadResult(taskId, true);
    }
  };
  source.onerror = () => source.close();
}

function startResultPolling(taskId) {
  stopResultPolling();
  resultPollTimer = setInterval(() => loadResult(taskId, true), 2000);
}

function stopResultPolling() {
  if (resultPollTimer) clearInterval(resultPollTimer);
  resultPollTimer = null;
}

async function uploadReview() {
  const files = $("files").files;
  if (!files.length) return alert("请选择要审查的文件");
  try {
    const encodedFiles = [];
    for (const file of files) encodedFiles.push(await readFileAsBase64(file));
    const res = await fetch("/api/review/upload", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({mode:currentMode, formats:$("formats").value, files:encodedFiles})
    });
    const payload = await parseResponse(res);
    if (!payload.ok) return alert(payload.error || "提交失败");
    connectSse(payload.task_id);
  } catch (error) {
    alert(error.message || "上传失败");
  }
}

async function pathReview() {
  const archiveDir = $("archiveDir").value.trim();
  if (!archiveDir) return alert("请填写服务器目录");
  try {
    const res = await fetch("/api/review/path", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({archive_dir:archiveDir, mode:currentMode, formats:$("formats").value}) });
    const payload = await parseResponse(res);
    if (!payload.ok) return alert(payload.error || "提交失败");
    connectSse(payload.task_id);
  } catch (error) {
    alert(error.message || "提交失败");
  }
}
$("uploadBtn").onclick = uploadReview;
$("pathBtn").onclick = pathReview;

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || "");
      resolve({name:file.name, type:file.type, size:file.size, data:value.split(",").pop()});
    };
    reader.onerror = () => reject(new Error(`读取文件失败：${file.name}`));
    reader.readAsDataURL(file);
  });
}

async function parseResponse(res) {
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return res.json();
  const text = await res.text();
  throw new Error(text || `请求失败：${res.status}`);
}

async function loadResult(taskId, jump) {
  const res = await fetch(`/api/review/result/${taskId}`);
  const payload = await res.json();
  if (!payload.ok) return;
  lastResult = payload;
  setSteps(payload.events || []);
  const summary = payload.summary || {};
  const task = payload.task || {};
  $("scoreValue").textContent = summary.score ?? task.score ?? "--";
  $("gradeValue").textContent = summary.grade || task.grade || "待评估";
  $("fileCount").textContent = summary.file_count || task.file_count || "--";
  $("issueCount").textContent = summary.issue_count ?? task.issue_count ?? "--";
  $("modeValue").textContent = task.mode || "--";
  $("logsPanel").textContent = payload.logs || payload.error || "";
  const issues = summary.issues || [];
  $("issuesPanel").innerHTML = issues.length ? issues.map(item => `<div class="card issue"><b>${escapeHtml(item.file)}</b><div>${escapeHtml(item.message)}</div><div class="muted">${escapeHtml(item.category)} · ${escapeHtml(item.severity)} ${item.evidence ? "· " + escapeHtml(item.evidence) : ""}</div></div>`).join("") : `<div class="card muted">暂无问题</div>`;
  if (task.status === "done") {
    stopResultPolling();
    if (jump || $("page-progress").classList.contains("active")) showPage("results");
  }
  if (task.status === "failed") {
    stopResultPolling();
    showPage("issues");
  }
}

$("htmlReportBtn").onclick = () => { if (currentTaskId) window.open(`/api/review/report/${currentTaskId}?fmt=html`, "_blank"); };
$("jsonReportBtn").onclick = () => { if (currentTaskId) window.open(`/api/review/report/${currentTaskId}?fmt=json`, "_blank"); };
$("issuesBtn").onclick = () => { if (currentTaskId) showPage("issues"); };

async function loadHistory() {
  const [historyRes, statsRes] = await Promise.all([fetch("/api/history"), fetch("/api/stats")]);
  const history = await historyRes.json();
  const stats = await statsRes.json();
  $("statTotal").textContent = stats.total ?? "--";
  $("statAvg").textContent = stats.avg_score ?? "--";
  $("statIssues").textContent = stats.issue_total ?? "--";
  const items = history.items || [];
  $("historyList").innerHTML = items.length ? items.map(item => `<div class="card" onclick="currentTaskId='${item.id}';localStorage.setItem('currentTaskId','${item.id}');loadResult('${item.id}',true)"><div class="row"><b>${escapeHtml(item.files?.[0] || item.id)}</b><span class="badge ${item.status === "done" ? "ok" : item.status === "failed" ? "bad" : "warn"}">${escapeHtml(item.status)}</span></div><div class="muted">${escapeHtml(item.created_at || "")} · ${item.score ?? "--"}分 · ${item.issue_count || 0}个问题</div></div>`).join("") : `<div class="card muted">暂无历史记录</div>`;
  $("recentList").innerHTML = items.slice(0,2).map(item => `<div class="card"><div class="row"><b>${escapeHtml(item.files?.[0] || item.id)}</b><span class="badge ok">${item.score ?? "--"}分</span></div><div class="muted">${escapeHtml(item.created_at || "")}</div></div>`).join("") || `<div class="card muted">暂无历史记录</div>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));
}

fetch("/api/health").then(r => r.json()).then(data => {
  $("health").textContent = "在线";
  if (!data.llm_configured) {
    document.querySelectorAll("#modeTabs button").forEach(button => {
      if (button.dataset.mode !== "code") {
        button.disabled = true;
        button.title = "未配置 LLM_API_KEY";
      }
    });
  }
}).catch(() => $("health").textContent = "离线");
loadHistory();
if (currentTaskId) loadResult(currentTaskId, true);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
</script>
</body>
</html>
"""


WEB_MANIFEST = json.dumps(
    {
        "name": "毕业设计智能审查系统",
        "short_name": "毕设审查",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f1f5f9",
        "theme_color": "#2563eb",
        "icons": [],
    },
    ensure_ascii=False,
)


SERVICE_WORKER = """
self.addEventListener("install", event => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", event => {});
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Mobile Web/PWA server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    init_db()
    server = QuietThreadingHTTPServer((args.host, args.port), ReviewHandler)
    print(f"Web/PWA server: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
