from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
from collections import Counter, deque
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

try:
    import aiohttp
except ModuleNotFoundError as exc:
    raise RuntimeError(
        "Missing dependency 'aiohttp'. Install with: pip install -r requirements.txt"
    ) from exc

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 Edg/124.0"
    ),
]

HIGH_TRUST_DOMAINS = {
    "who.int",
    "cdc.gov",
    "nhs.uk",
    "mayoclinic.org",
    "medlineplus.gov",
    "nih.gov",
    "healthline.com",
    "clevelandclinic.org",
    "webmd.com",
    "hopkinsmedicine.org",
    "patient.info",
    "msdmanuals.com",
    "emro.who.int",
}

LOW_TRUST_HINTS = {
    "forum",
    "reddit",
    "quora",
    "blogspot",
    "pinterest",
    "facebook",
    "twitter",
    "x.com",
    "tiktok",
}

SEVERITY_LABELS = {
    1: "mild",
    2: "normal",
    3: "moderate",
    4: "severe",
    5: "critical",
}

UTF8_BOM = b"\xef\xbb\xbf"

CSV_FIELDS = [
    "disease_bn",
    "disease_en",
    "symptom_bn",
    "symptom_en",
    "severity_level",
    "severity_label",
    "action_mild_bn",
    "action_mild_en",
    "action_severe_bn",
    "action_severe_en",
]

MAX_HEADER_LINE = 16384
MAX_HEADER_FIELD = 16384

IGNORE_TAGS = {"script", "style", "noscript", "svg"}

LOGGER = logging.getLogger("symptom_agent")


@dataclass
class PageData:
    url: str
    text: str
    links: List[str]
    score: int


class HTMLExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._ignore_depth = 0
        self.text_chunks: List[str] = []
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in IGNORE_TAGS:
            self._ignore_depth += 1
            return
        if tag == "a":
            attr_map = dict(attrs)
            href = attr_map.get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in IGNORE_TAGS and self._ignore_depth > 0:
            self._ignore_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignore_depth == 0:
            self.text_chunks.append(data)


class SearchLinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag != "a":
            return
        attr_map = dict(attrs)
        href = attr_map.get("href")
        css_class = attr_map.get("class", "") or ""
        if href and ("result__a" in css_class or "result-link" in css_class):
            self.links.append(href)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def normalize_whitespace(text: str) -> str:
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_text(text: str) -> str:
    text = normalize_whitespace(text)
    text = re.sub(r"\s*\|\s*", " | ", text)
    return text.strip()


def normalize_action_text(value: object) -> str:
    return clean_text(str(value or "")).strip()


def ensure_utf8_bom(path: str) -> None:
    try:
        with open(path, "rb") as handle:
            start = handle.read(3)
            if start == UTF8_BOM:
                return
        temp_path = f"{path}.tmp"
        with open(path, "rb") as src, open(temp_path, "wb") as dst:
            dst.write(UTF8_BOM)
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(temp_path, path)
    except OSError as exc:
        LOGGER.warning("Failed to add UTF-8 BOM to %s: %s", path, exc)


def is_valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    cleaned = parsed._replace(fragment="")
    return cleaned.geturl()


def is_probably_binary(url: str) -> bool:
    return bool(re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|mp3)($|\?)", url))


def domain_score(url: str) -> int:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    score = 0
    if domain in HIGH_TRUST_DOMAINS or any(domain.endswith("." + d) for d in HIGH_TRUST_DOMAINS):
        score += 5
    if any(k in domain for k in ["health", "med", "clinic", "hospital", "nih", "gov", "edu"]):
        score += 2
    if any(hint in domain for hint in LOW_TRUST_HINTS):
        score -= 4
    path = parsed.path.lower()
    if any(k in path for k in ["symptom", "sign", "complication", "warning", "emergency"]):
        score += 2
    return score


def prioritize_urls(urls: Iterable[str]) -> List[str]:
    scored = []
    for url in urls:
        score = domain_score(url)
        scored.append((score, url))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [url for _, url in scored]


def resolve_ddg_redirect(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        query = parse_qs(parsed.query)
        if "uddg" in query:
            return unquote(query["uddg"][0])
    return url


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*]+", "_", name.strip())
    return cleaned or "dataset"


def chunk_texts(texts: List[str], max_chars: int) -> List[str]:
    chunks: List[str] = []
    current = ""
    for text in texts:
        if not text:
            continue
        if len(current) + len(text) + 2 > max_chars:
            if current:
                chunks.append(current)
            current = text
        else:
            current = f"{current}\n\n{text}" if current else text
    if current:
        chunks.append(current)
    return chunks


def normalize_rows(rows: List[Dict[str, object]], disease_en: str, disease_bn: str) -> List[Dict[str, object]]:
    normalized: List[Dict[str, object]] = []
    for row in rows:
        try:
            level = int(row.get("severity_level", 0))
        except (TypeError, ValueError):
            continue
        if level not in SEVERITY_LABELS:
            continue
        symptom_bn = str(row.get("symptom_bn", "")).strip()
        symptom_en = str(row.get("symptom_en", "")).strip()
        if not symptom_bn or not symptom_en:
            continue
        label = str(row.get("severity_label", "")).strip().lower()
        label = SEVERITY_LABELS.get(level, label)
        action_mild_bn = normalize_action_text(row.get("action_mild_bn", ""))
        action_mild_en = normalize_action_text(row.get("action_mild_en", ""))
        action_severe_bn = normalize_action_text(row.get("action_severe_bn", ""))
        action_severe_en = normalize_action_text(row.get("action_severe_en", ""))
        normalized.append(
            {
                "disease_bn": str(row.get("disease_bn", "") or disease_bn).strip(),
                "disease_en": str(row.get("disease_en", "") or disease_en).strip(),
                "symptom_bn": symptom_bn,
                "symptom_en": symptom_en,
                "severity_level": level,
                "severity_label": label,
                "action_mild_bn": action_mild_bn,
                "action_mild_en": action_mild_en,
                "action_severe_bn": action_severe_bn,
                "action_severe_en": action_severe_en,
            }
        )
    return normalized


def dedupe_rows(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    seen: Set[Tuple[str, str, int]] = set()
    unique: List[Dict[str, object]] = []
    for row in rows:
        key = (
            str(row["disease_en"]).casefold(),
            str(row["symptom_en"]).casefold(),
            int(row["severity_level"]),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def get_unique_symptom_count(rows: List[Dict[str, object]]) -> int:
    return len({str(row["symptom_en"]).casefold() for row in rows})


def extract_text_and_links(html: str, base_url: str) -> Tuple[str, List[str]]:
    extractor = HTMLExtractor()
    extractor.feed(html)
    text = clean_text(" ".join(extractor.text_chunks))
    links: List[str] = []
    for href in extractor.links:
        resolved = urljoin(base_url, href)
        if not is_valid_url(resolved):
            continue
        normalized = normalize_url(resolved)
        if is_probably_binary(normalized):
            continue
        links.append(normalized)
    return text, links


def parse_search_results(html: str) -> List[str]:
    extractor = SearchLinkExtractor()
    extractor.feed(html)
    urls = []
    for href in extractor.links:
        resolved = resolve_ddg_redirect(href)
        if not is_valid_url(resolved):
            continue
        if is_probably_binary(resolved):
            continue
        urls.append(normalize_url(resolved))
    return urls


def build_prompt(disease_en: str, text: str) -> str:
    return (
        "You are a medical data extractor. Based ONLY on the provided text, "
        "extract symptoms for the disease and classify each symptom into one "
        "severity level. Output JSON ONLY as an array of objects with keys: "
        "disease_bn, disease_en, symptom_bn, symptom_en, severity_level, severity_label, "
        "action_mild_bn, action_mild_en, action_severe_bn, action_severe_en. "
        "Rules: severity_level must be 1-5, severity_label must be one of "
        "mild, normal, moderate, severe, critical. Provide Bangla text first "
        "then English. Normalize duplicates and synonyms. Avoid non-symptoms. "
        "action_mild = what to do if symptoms are mild/light. "
        "action_severe = what to do if symptoms are severe or critical. "
        "Keep actions short, general, and safe (no dosages or prescriptions). "
        f"Disease (English): {disease_en}.\n\n"
        "Text:\n"
        f"{text}"
    )


def build_disease_translation_prompt(disease_en: str) -> str:
    return (
        "Translate the disease name to Bangla. Output JSON ONLY as: "
        '{"disease_bn": "...", "disease_en": "..."}. '
        f"Disease (English): {disease_en}"
    )


def parse_json_from_text(text: str) -> Optional[object]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: try to extract the first JSON array or object.
    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start != -1 and array_end != -1 and array_end > array_start:
        try:
            return json.loads(text[array_start : array_end + 1])
        except json.JSONDecodeError:
            return None
    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
        try:
            return json.loads(text[obj_start : obj_end + 1])
        except json.JSONDecodeError:
            return None
    return None


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    max_retries: int,
) -> Optional[Tuple[str, str]]:
    for attempt in range(max_retries):
        try:
            async with session.get(
                url,
                headers={"User-Agent": random.choice(USER_AGENTS)},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status in {429, 500, 502, 503, 504}:
                    raise aiohttp.ClientResponseError(
                        response.request_info,
                        response.history,
                        status=response.status,
                        message="retryable",
                        headers=response.headers,
                    )
                content_type = response.headers.get("Content-Type", "")
                body = await response.text(errors="ignore")
                return content_type, body
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == max_retries - 1:
                LOGGER.warning("Fetch failed for %s: %s", url, exc)
                return None
            sleep_for = (0.5 * (2**attempt)) + random.uniform(0.0, 0.2)
            await asyncio.sleep(sleep_for)
    return None


async def search_duckduckgo(
    session: aiohttp.ClientSession,
    query: str,
    limit: int,
    timeout: int,
    max_retries: int,
) -> List[str]:
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    result = await fetch_text(session, url, timeout, max_retries)
    if not result:
        return []
    _, html = result
    urls = parse_search_results(html)
    seen: Set[str] = set()
    filtered: List[str] = []
    for candidate in urls:
        if candidate in seen:
            continue
        seen.add(candidate)
        filtered.append(candidate)
        if len(filtered) >= limit:
            break
    return filtered


async def fetch_page_data(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    max_retries: int,
    min_text_len: int,
) -> Optional[PageData]:
    result = await fetch_text(session, url, timeout, max_retries)
    if not result:
        return None
    content_type, html = result
    if "text/html" not in content_type:
        return None
    text, links = extract_text_and_links(html, url)
    if len(text) < min_text_len:
        return None
    return PageData(url=url, text=text, links=links, score=domain_score(url))


async def crawl(
    session: aiohttp.ClientSession,
    seed_urls: List[str],
    max_pages: int,
    max_depth: int,
    concurrency: int,
    timeout: int,
    max_retries: int,
    min_text_len: int,
) -> List[PageData]:
    queue: deque[Tuple[str, int]] = deque((url, 0) for url in seed_urls)
    visited: Set[str] = set()
    pages: List[PageData] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded_fetch(url: str, depth: int) -> Tuple[str, int, Optional[PageData]]:
        async with semaphore:
            data = await fetch_page_data(session, url, timeout, max_retries, min_text_len)
            return url, depth, data

    while queue and len(pages) < max_pages:
        batch: List[Tuple[str, int]] = []
        while queue and len(batch) < concurrency and len(pages) + len(batch) < max_pages:
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            batch.append((url, depth))

        tasks = [guarded_fetch(url, depth) for url, depth in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                continue
            url, depth, page = result
            if not page:
                continue
            pages.append(page)
            if depth >= max_depth:
                continue
            candidates = prioritize_urls(page.links)
            for link in candidates[:20]:
                if link in visited:
                    continue
                if domain_score(link) < 0:
                    continue
                queue.append((link, depth + 1))

    return pages


async def call_openrouter(
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int,
    max_retries: int,
) -> Optional[str]:
    url = "https://openrouter.ai/api/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 2048,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://localhost",
        "X-Title": "symptom-dataset-agent",
    }

    for attempt in range(max_retries):
        try:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status in {429, 500, 502, 503, 504}:
                    raise aiohttp.ClientResponseError(
                        response.request_info,
                        response.history,
                        status=response.status,
                        message="retryable",
                        headers=response.headers,
                    )
                data = await response.json()
                choices = data.get("choices", [])
                if not choices:
                    return None
                message = choices[0].get("message", {})
                content = message.get("content")
                if not content:
                    return None
                return content
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            if attempt == max_retries - 1:
                LOGGER.warning("OpenRouter request failed: %s", exc)
                return None
            sleep_for = (0.6 * (2**attempt)) + random.uniform(0.0, 0.2)
            await asyncio.sleep(sleep_for)
    return None


async def translate_disease_name(
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    disease_en: str,
    timeout: int,
    max_retries: int,
) -> str:
    prompt = build_disease_translation_prompt(disease_en)
    response_text = await call_openrouter(
        session, api_key, model, prompt, timeout, max_retries
    )
    if not response_text:
        return ""
    data = parse_json_from_text(response_text)
    if isinstance(data, dict):
        return str(data.get("disease_bn", "")).strip()
    return ""


async def extract_symptoms_from_texts(
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    disease_en: str,
    texts: List[str],
    max_chars: int,
    timeout: int,
    max_retries: int,
) -> List[Dict[str, object]]:
    chunks = chunk_texts(texts, max_chars)
    rows: List[Dict[str, object]] = []

    for idx, chunk in enumerate(chunks, start=1):
        LOGGER.info("OpenRouter extraction chunk %s/%s", idx, len(chunks))
        prompt = build_prompt(disease_en, chunk)
        response_text = await call_openrouter(
            session, api_key, model, prompt, timeout, max_retries
        )
        if not response_text:
            continue
        data = parse_json_from_text(response_text)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    rows.append(item)
        elif isinstance(data, dict):
            rows.append(data)
    return rows


def append_csv(output_path: str, rows: List[Dict[str, object]]) -> None:
    file_exists = os.path.exists(output_path)
    write_header = not file_exists or os.path.getsize(output_path) == 0
    if file_exists and not write_header:
        ensure_utf8_bom(output_path)
    mode = "w" if write_header else "a"
    encoding = "utf-8-sig" if write_header else "utf-8"
    with open(output_path, mode, newline="", encoding=encoding) as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def build_search_queries(disease_en: str) -> List[str]:
    return [
        f"{disease_en} symptoms",
        f"{disease_en} signs and symptoms",
        f"{disease_en} complications",
        f"{disease_en} severe symptoms",
        f"{disease_en} warning signs",
    ]


def choose_disease_bn(rows: List[Dict[str, object]]) -> str:
    candidates = [row.get("disease_bn", "") for row in rows if row.get("disease_bn")]
    if not candidates:
        return ""
    counts = Counter(str(item).strip() for item in candidates if str(item).strip())
    return counts.most_common(1)[0][0] if counts else ""


async def run_agent(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OpenRouter API key missing. Use --api-key or OPENROUTER_API_KEY env var."
        )

    disease_en = args.disease.strip()
    search_queries = build_search_queries(disease_en)
    all_rows: List[Dict[str, object]] = []
    seen_urls: Set[str] = set()

    async with aiohttp.ClientSession(
        max_line_size=MAX_HEADER_LINE,
        max_field_size=MAX_HEADER_FIELD,
    ) as session:
        disease_bn = ""
        for query in search_queries:
            LOGGER.info("Search query: %s", query)
            seed_urls = await search_duckduckgo(
                session,
                query,
                args.search_limit,
                args.timeout,
                args.max_retries,
            )
            seed_urls = [url for url in seed_urls if url not in seen_urls]
            seen_urls.update(seed_urls)
            prioritized = prioritize_urls(seed_urls)

            if not prioritized:
                continue

            pages = await crawl(
                session,
                prioritized,
                args.max_pages,
                args.max_depth,
                args.concurrency,
                args.timeout,
                args.max_retries,
                args.min_text_len,
            )
            if not pages:
                continue

            texts = [page.text for page in pages]
            raw_rows = await extract_symptoms_from_texts(
                session,
                api_key,
                args.model,
                disease_en,
                texts,
                args.max_chars,
                args.timeout,
                args.max_retries,
            )
            if not raw_rows:
                continue

            if not disease_bn:
                disease_bn = choose_disease_bn(raw_rows)
            normalized = normalize_rows(raw_rows, disease_en, disease_bn)
            all_rows.extend(normalized)
            all_rows = dedupe_rows(all_rows)

            if get_unique_symptom_count(all_rows) >= args.min_symptoms:
                break

        if not all_rows:
            raise RuntimeError("No symptom data extracted. Try increasing limits or queries.")

        if not disease_bn:
            disease_bn = await translate_disease_name(
                session,
                api_key,
                args.model,
                disease_en,
                args.timeout,
                args.max_retries,
            )
            all_rows = normalize_rows(all_rows, disease_en, disease_bn)
            all_rows = dedupe_rows(all_rows)

    output_path = os.path.join(args.output_dir, args.output_file)
    append_csv(output_path, all_rows)

    LOGGER.info("Appended CSV: %s", output_path)
    LOGGER.info("Rows added: %s", len(all_rows))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate symptom severity dataset using web crawl + OpenRouter."
    )
    parser.add_argument("disease", help="Disease name in English, e.g., 'fever'")
    parser.add_argument("--api-key", help="OpenRouter API key (or set OPENROUTER_API_KEY)")
    parser.add_argument(
        "--model",
        default="tencent/hy3-preview:free",
        help="OpenRouter model id",
    )
    parser.add_argument("--output-dir", default=".", help="Output directory")
    parser.add_argument("--min-symptoms", type=int, default=20, help="Target unique symptoms")
    parser.add_argument("--max-pages", type=int, default=20, help="Max pages per query")
    parser.add_argument("--max-depth", type=int, default=1, help="Crawler depth")
    parser.add_argument("--search-limit", type=int, default=10, help="Search results per query")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrent requests")
    parser.add_argument("--max-chars", type=int, default=6000, help="Max chars per AI chunk")
    parser.add_argument("--timeout", type=int, default=25, help="Request timeout seconds")
    parser.add_argument("--max-retries", type=int, default=3, help="Retry count")
    parser.add_argument("--min-text-len", type=int, default=500, help="Min page text length")
    parser.add_argument("--output-file", default="adata.csv", help="Output CSV filename")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)
    try:
        asyncio.run(run_agent(args))
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted by user")


if __name__ == "__main__":
    main()
