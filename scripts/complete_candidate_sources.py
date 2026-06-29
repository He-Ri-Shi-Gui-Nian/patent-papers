#!/usr/bin/env python3
import argparse
import gzip
import html
import json
import os
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


WORKSPACE = Path.cwd()
ARXIV_TEX = WORKSPACE / "arxiv_tex"
STATE_DIR = WORKSPACE / "candidate_work"
CANDIDATES_FILE = STATE_DIR / "all_candidates.json"
DOWNLOAD_STATUS_FILE = STATE_DIR / "download_status.json"
ABSTRACTS_FILE = STATE_DIR / "abstracts_from_tex.json"
TMP_DIR = STATE_DIR / "tmp"

ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV = "{http://arxiv.org/schemas/atom}"
USER_AGENT = "CodexArxivCandidateSourceDownloader/1.0"

DEFAULT_INTERVAL = 6.5


def ensure_dirs():
    STATE_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    ARXIV_TEX.mkdir(exist_ok=True)


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_space(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def strip_version(arxiv_id):
    return re.sub(r"v\d+$", "", arxiv_id)


def safe_id(arxiv_id):
    return strip_version(arxiv_id).replace("/", "_")


def parse_feed(xml):
    root = ET.fromstring(xml)
    entries = []
    total_elem = root.find(f"{OPENSEARCH}totalResults")
    total = int(total_elem.text) if total_elem is not None and total_elem.text else None
    for entry in root.findall(f"{ATOM}entry"):
        title = normalize_space(entry.findtext(f"{ATOM}title"))
        if title.lower() == "error":
            continue
        entry_id = normalize_space(entry.findtext(f"{ATOM}id"))
        arxiv_id = strip_version(entry_id.rsplit("/abs/", 1)[-1] if "/abs/" in entry_id else entry_id)
        authors = []
        for author in entry.findall(f"{ATOM}author"):
            name = normalize_space(author.findtext(f"{ATOM}name"))
            if name:
                authors.append(name)
        categories = []
        for category in entry.findall(f"{ATOM}category"):
            term = category.attrib.get("term")
            if term:
                categories.append(term)
        primary = entry.find(f"{ARXIV}primary_category")
        entries.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": authors,
                "published": normalize_space(entry.findtext(f"{ATOM}published")),
                "updated": normalize_space(entry.findtext(f"{ATOM}updated")),
                "metadata_abstract": normalize_space(entry.findtext(f"{ATOM}summary")),
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "primary_category": primary.attrib.get("term") if primary is not None else None,
                "categories": categories,
            }
        )
    return total, entries


def fetch_url(url, interval, retries=4):
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(1, retries + 1):
        if interval:
            time.sleep(interval)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                try:
                    wait = int(retry_after) if retry_after else 90 * attempt
                except ValueError:
                    wait = 90 * attempt
                print(f"HTTP 429; waiting {wait}s before retry {attempt}/{retries}: {url}", flush=True)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
            time.sleep(20 * attempt)


def rebuild_candidates(interval):
    keywords = load_json(WORKSPACE / "keywords_expanded.json", {})
    existing_selected = load_json(WORKSPACE / "papers.json", {})
    by_task = {}
    for task_id, task_info in keywords.items():
        by_id = {}
        history = task_info.get("query_history", [])
        for q in history:
            url = q.get("url")
            if not url:
                query = q.get("query")
                if not query:
                    continue
                params = {
                    "search_query": query,
                    "start": "0",
                    "max_results": "15",
                    "sortBy": "relevance",
                    "sortOrder": "descending",
                }
                url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
            print(f"Fetching metadata for {task_id}: {q.get('query', url)}", flush=True)
            xml = fetch_url(url, interval).decode("utf-8", errors="replace")
            _, entries = parse_feed(xml)
            for paper in entries:
                item = by_id.setdefault(paper["arxiv_id"], paper)
                item.setdefault("query_hits", [])
                item["query_hits"].append(q.get("query", url))
        # Ensure the 60 already selected papers remain included even if a previous query changes.
        for paper in existing_selected.get(task_id, {}).get("papers", []):
            arxiv_id = strip_version(paper["arxiv_id_base"])
            by_id.setdefault(
                arxiv_id,
                {
                    "arxiv_id": arxiv_id,
                    "title": paper.get("title", ""),
                    "authors": paper.get("authors", []),
                    "published": paper.get("published", ""),
                    "updated": paper.get("updated", ""),
                    "metadata_abstract": paper.get("summary", ""),
                    "url": paper.get("url", f"https://arxiv.org/abs/{arxiv_id}"),
                    "primary_category": paper.get("primary_category"),
                    "categories": paper.get("categories", []),
                    "query_hits": ["selected_from_previous_run"],
                },
            )
        by_task[task_id] = sorted(by_id.values(), key=lambda p: (p.get("updated", ""), p["arxiv_id"]), reverse=True)
    write_json(CANDIDATES_FILE, by_task)
    print("Candidate counts:", {k: len(v) for k, v in by_task.items()}, flush=True)
    return by_task


def has_tex_files(dest):
    return any(p.suffix.lower() in {".tex", ".ltx"} for p in dest.rglob("*") if p.is_file())


def safe_extract_tar(archive, dest):
    resolved = dest.resolve()
    with tarfile.open(archive, mode="r:*") as tar:
        members = tar.getmembers()
        for member in members:
            target = (dest / member.name).resolve()
            if target != resolved and not str(target).startswith(str(resolved) + os.sep):
                raise RuntimeError(f"unsafe tar member: {member.name}")
        tar.extractall(dest, members=members, filter="data")


def write_single_source(raw_path, dest):
    raw = raw_path.read_bytes()
    try:
        data = gzip.decompress(raw)
    except OSError:
        data = raw
    if b"\\documentclass" in data or b"\\begin{abstract}" in data or b"\\begin{document}" in data:
        (dest / "source.tex").write_bytes(data)
    elif data.startswith(b"%PDF"):
        (dest / "source.pdf").write_bytes(data)
    else:
        (dest / "source.dat").write_bytes(data)


def download_one(task_id, arxiv_id, interval):
    sid = safe_id(arxiv_id)
    dest = ARXIV_TEX / task_id / sid
    if dest.exists() and has_tex_files(dest):
        return {"status": "already_downloaded", "source_dir": str(dest.relative_to(WORKSPACE)), "has_tex": True}
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    tmp = TMP_DIR / f"{task_id}_{sid}.src"
    url = f"https://arxiv.org/src/{strip_version(arxiv_id)}"
    try:
        tmp.write_bytes(fetch_url(url, interval))
        try:
            safe_extract_tar(tmp, dest)
        except tarfile.TarError:
            write_single_source(tmp, dest)
        has_tex = has_tex_files(dest)
        return {
            "status": "downloaded" if has_tex else "downloaded_no_tex_detected",
            "source_dir": str(dest.relative_to(WORKSPACE)),
            "source_url": url,
            "has_tex": has_tex,
        }
    except Exception as exc:
        if dest.exists() and not any(dest.iterdir()):
            dest.rmdir()
        return {
            "status": "failed",
            "source_dir": str(dest.relative_to(WORKSPACE)),
            "source_url": url,
            "has_tex": False,
            "error": str(exc),
        }
    finally:
        if tmp.exists():
            tmp.unlink()


def download_all(candidates, interval, limit=None):
    status = load_json(DOWNLOAD_STATUS_FILE, {})
    for task_id, papers in candidates.items():
        task_status = status.setdefault(task_id, {})
        done = sum(1 for v in task_status.values() if v.get("has_tex"))
        print(f"Download pass for {task_id}: {done}/{len(papers)} already have TeX", flush=True)
        count = 0
        for paper in papers:
            arxiv_id = paper["arxiv_id"]
            current = task_status.get(arxiv_id)
            if current and current.get("has_tex"):
                continue
            count += 1
            if limit and count > limit:
                break
            print(f"Downloading {task_id} {arxiv_id} ({count} pending in this pass)", flush=True)
            result = download_one(task_id, arxiv_id, interval)
            task_status[arxiv_id] = result
            write_json(DOWNLOAD_STATUS_FILE, status)
    return status


def strip_comments(text):
    out = []
    for line in text.splitlines():
        escaped = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == "\\":
                escaped = not escaped
                continue
            if ch == "%" and not escaped:
                cut = i
                break
            escaped = False
        out.append(line[:cut])
    return "\n".join(out)


def clean_latex(text):
    text = strip_comments(text)
    text = re.sub(r"\\(?:cite|citep|citet|citealp|ref|cref|Cref|label|url|href)\*?(?:\[[^\]]*\])?\{[^{}]*\}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{([^{}]*)\})?", lambda m: m.group(1) or " ", text)
    text = re.sub(r"[{}$&_#^~]", " ", text)
    return normalize_space(text)


def read_text(path):
    for enc in ["utf-8", "latin-1"]:
        try:
            return path.read_text(encoding=enc, errors="ignore")
        except Exception:
            pass
    return ""


def find_main_text(dest):
    tex_files = list(dest.rglob("*.tex")) + list(dest.rglob("*.ltx"))
    scored = []
    for path in tex_files:
        text = read_text(path)
        if not text:
            continue
        score = len(text) / 10000
        if "\\documentclass" in text:
            score += 1000
        if "\\begin{document}" in text:
            score += 500
        if "\\begin{abstract}" in text:
            score += 500
        score += len(re.findall(r"\\section\*?\{", text)) * 10
        scored.append((score, path, text))
    scored.sort(reverse=True, key=lambda x: x[0])
    if not scored:
        return "", ""
    main_path, main_text = scored[0][1], scored[0][2]
    parts = [main_text]
    for name in re.findall(r"\\(?:input|include)\{([^{}]+)\}", main_text):
        child = main_path.parent / name
        if not child.suffix:
            child = child.with_suffix(".tex")
        if child.exists() and child.is_file():
            parts.append(read_text(child))
    return str(main_path), "\n".join(parts)


def extract_abstract_from_text(text):
    if not text:
        return ""
    matches = re.findall(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", text, flags=re.S | re.I)
    if matches:
        best = max(matches, key=len)
        return clean_latex(best)
    # Some TeX sources use abstract macros instead of an environment.
    patterns = [
        r"\\abstract\{(.{80,4000}?)\}\s*\\(?:section|keywords|maketitle)",
        r"\\begin\{frontmatter\}.*?\\begin\{abstract\}(.*?)\\end\{abstract\}",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.S | re.I)
        if m:
            return clean_latex(m.group(1))
    return ""


def extract_abstract_from_dir(dest):
    main_tex, text = find_main_text(dest)
    abstract = extract_abstract_from_text(text)
    if abstract:
        return main_tex, abstract
    best_path = main_tex
    best_abstract = ""
    for path in list(dest.rglob("*.tex")) + list(dest.rglob("*.ltx")):
        text = read_text(path)
        abstract = extract_abstract_from_text(text)
        if len(abstract) > len(best_abstract):
            best_path = str(path)
            best_abstract = abstract
    return best_path, best_abstract


def extract_all_abstracts(candidates):
    status = load_json(DOWNLOAD_STATUS_FILE, {})
    output = {}
    for task_id, papers in candidates.items():
        out_papers = []
        for paper in papers:
            arxiv_id = paper["arxiv_id"]
            st = status.get(task_id, {}).get(arxiv_id, {})
            source_dir = st.get("source_dir") or str((ARXIV_TEX / task_id / safe_id(arxiv_id)).relative_to(WORKSPACE))
            dest = WORKSPACE / source_dir
            main_tex, tex_abstract = extract_abstract_from_dir(dest) if dest.exists() else ("", "")
            out_papers.append(
                {
                    **paper,
                    "source_dir": source_dir,
                    "main_tex": main_tex,
                    "download_status": st.get("status", "missing"),
                    "has_tex": bool(st.get("has_tex")),
                    "tex_abstract": tex_abstract,
                    "abstract_source": "tex" if tex_abstract else "missing_in_tex",
                }
            )
        output[task_id] = out_papers
    write_json(ABSTRACTS_FILE, output)
    print(
        "Abstract extraction counts:",
        {k: {"papers": len(v), "tex_abstracts": sum(1 for p in v if p.get("tex_abstract"))} for k, v in output.items()},
        flush=True,
    )
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-candidates", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--download-limit-per-task", type=int, default=None)
    args = parser.parse_args()
    ensure_dirs()
    if args.all or args.rebuild_candidates or not CANDIDATES_FILE.exists():
        candidates = rebuild_candidates(args.interval)
    else:
        candidates = load_json(CANDIDATES_FILE, {})
    if args.all or args.download:
        download_all(candidates, args.interval, args.download_limit_per_task)
    if args.all or args.extract:
        extract_all_abstracts(candidates)


if __name__ == "__main__":
    main()
