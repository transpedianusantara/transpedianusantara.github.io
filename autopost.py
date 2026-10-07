import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

SITE = "https://transpedia.id/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Transpedia-Nusantara-Autopost/1.0)"
}


def get(url, timeout=30):
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def latest_from_feed():
    candidates = []

    for feed_url in (urljoin(SITE, "feed/"), urljoin(SITE, "rss/")):
        try:
            xml = get(feed_url, timeout=20)
            root = ET.fromstring(xml)

            for item in root.findall(".//item"):
                title = clean(item.findtext("title"))
                link = clean(item.findtext("link"))
                pub_date = clean(item.findtext("pubDate"))

                if not title or not link:
                    continue

                try:
                    published = datetime.strptime(
                        pub_date, "%a, %d %b %Y %H:%M:%S %z"
                    )
                except ValueError:
                    published = datetime.min.replace(tzinfo=timezone.utc)

                candidates.append((published, link, title))

            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                return candidates[0][1], candidates[0][2]

        except Exception as exc:
            print(f"Feed gagal: {feed_url} -> {exc}")

    return None


def latest_from_homepage():
    html = get(SITE)
    soup = BeautifulSoup(html, "html.parser")

    candidates = []

    # WordPress pages normally expose publication dates through article cards,
    # links, time elements, or surrounding containers.
    for article in soup.find_all("article"):
        link_tag = article.find("a", href=True)
        if not link_tag:
            continue

        url = urljoin(SITE, link_tag["href"]).split("#")[0]
        title_tag = article.find(["h1", "h2", "h3", "h4"]) or link_tag
        title = clean(title_tag.get_text(" ", strip=True))

        if not url.startswith(SITE) or len(title) < 10:
            continue

        date_tag = article.find("time")
        date_text = clean(date_tag.get("datetime") if date_tag else "")
        if not date_text and date_tag:
            date_text = clean(date_tag.get_text(" ", strip=True))

        published = datetime.min.replace(tzinfo=timezone.utc)

        if date_text:
            try:
                published = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
            except ValueError:
                for fmt in ("%B %d, %Y", "%d %B %Y"):
                    try:
                        published = datetime.strptime(date_text, fmt).replace(
                            tzinfo=timezone.utc
                        )
                        break
                    except ValueError:
                        pass

        candidates.append((published, url, title))

    if not candidates:
        raise RuntimeError("Tidak menemukan artikel terbaru di Transpedia.id.")

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, url, title = candidates[0]
    return url, title


def get_latest_article():
    result = latest_from_feed()
    if result:
        return result
    return latest_from_homepage()


def extract_article(url, fallback_title):
    html = get(url)
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else fallback_title

    article = soup.find("article") or soup.find("main")
    if article is None:
        raise RuntimeError("Elemen artikel tidak ditemukan.")

    for tag in article.find_all(
        ["script", "style", "nav", "footer", "form", "noscript"]
    ):
        tag.decompose()

    text = article.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text[:30000]

    return title, text


def already_processed(source_url):
    marker = f"source_url: {source_url}"

    for post in Path("_posts").glob("*.md"):
        try:
            if marker in post.read_text(encoding="utf-8"):
                print(f"SKIP: sumber sudah diproses -> {post}")
                return True
        except UnicodeDecodeError:
            pass

    return False


def generate_article(source_title, source_url, source_text):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Secret OPENAI_API_KEY belum tersedia.")

    prompt = f"""
Kamu adalah editor senior Transpedia Nusantara.

Buat SATU artikel referensi baru berdasarkan artikel sumber berikut.
Artikel baru harus punya sudut pandang sendiri. Jangan menyalin atau
memparafrase kalimat sumber satu per satu.

ATURAN:
- Bahasa Indonesia natural, editorial, dan manusiawi.
- Profesional tetapi tidak kaku.
- Hindari gaya AI yang generik, repetitif, dan penuh filler.
- Jangan mengarang angka, fakta, kutipan, atau sumber.
- Gunakan hanya informasi yang dapat didukung oleh artikel sumber.
- Berikan analisis dan konteks praktis yang berbeda dari artikel sumber.
- Pembuka langsung menjawab isu utama.
- Gunakan heading H2/H3 jika memang membantu.
- Target 900-1300 kata.
- Jangan gunakan em dash.
- Jangan menyebut AI atau proses pembuatan artikel.
- Di akhir, berikan satu paragraf yang mengaitkan pembahasan dengan
  artikel sumber dan tautkan sumber secara natural.
- Jangan membuat daftar pustaka palsu.

Kembalikan JSON VALID dengan tepat tiga field:
title
description
body

JUDUL SUMBER:
{source_title}

URL SUMBER:
{source_url}

ISI SUMBER:
{source_text}
"""

    payload = {
        "model": "gpt-6-luna",
        "input": prompt,
        "max_output_tokens": 5000,
    }

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    response.raise_for_status()

    data = response.json()
    output_text = data.get("output_text", "").strip()

    if not output_text:
        parts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
        output_text = "\n".join(parts).strip()

    if not output_text:
        raise RuntimeError("Responses API tidak mengembalikan teks.")

    if output_text.startswith("```"):
        output_text = re.sub(r"^```(?:json)?\s*", "", output_text)
        output_text = re.sub(r"\s*```$", "", output_text)

    return json.loads(output_text)


def main():
    source_url, fallback_title = get_latest_article()

    print(f"SOURCE_URL={source_url}")
    print(f"SOURCE_TITLE={fallback_title}")

    if already_processed(source_url):
        return

    source_title, source_text = extract_article(source_url, fallback_title)

    result = generate_article(source_title, source_url, source_text)

    title = clean(result["title"])
    description = clean(result["description"])
    body = result["body"].strip()

    if not title or not body:
        raise RuntimeError("Output AI tidak memiliki title/body yang valid.")

    now = datetime.now(timezone.utc)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:90]

    posts_dir = Path("_posts")
    posts_dir.mkdir(exist_ok=True)

    post_path = posts_dir / f"{now:%Y-%m-%d}-{slug}.md"

    front_matter = (
        "---\n"
        "layout: default\n"
        f'title: "{title.replace(chr(34), chr(92)+chr(34))}"\n'
        f'description: "{description.replace(chr(34), chr(92)+chr(34))}"\n'
        f"date: {now:%Y-%m-%d %H:%M:%S %z}\n"
        "categories: [insights]\n"
        f"source_url: {source_url}\n"
        f'source_title: "{source_title.replace(chr(34), chr(92)+chr(34))}"\n'
        "---\n\n"
    )

    post_path.write_text(front_matter + body + "\n", encoding="utf-8")

    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        check=True,
    )
    subprocess.run(["git", "add", str(post_path)], check=True)

    subprocess.run(
        ["git", "commit", "-m", f"Autopost: {title}"],
        check=True,
    )
    subprocess.run(["git", "push"], check=True)

    print(f"CREATED={post_path}")
    print(f"TITLE={title}")
    print(f"SOURCE={source_url}")


if __name__ == "__main__":
    main()
