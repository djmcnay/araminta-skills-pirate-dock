import httpx
from bs4 import BeautifulSoup
import urllib.parse
import json

def search(query):
    q = urllib.parse.quote(query)
    url = f"https://annas-archive.gl/search?q={q}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    try:
        r = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        
        soup = BeautifulSoup(r.text, "lxml")
        results = []
        container = soup.select_one(".js-aarecord-list-outer")
        if container:
            rows = [c for c in container.children if hasattr(c, 'get') and 'border-b' in ' '.join(c.get('class', []))]
            for row in rows:
                link = row.select_one("a[href*='/md5/']")
                if not link: continue
                md5 = link['href'].split('/')[-1]
                title_el = row.select_one("h3")
                title = title_el.get_text(strip=True) if title_el else row.get_text(separator=' ', strip=True)[:100]
                results.append({"md5": md5, "title": title, "url": f"https://annas-archive.gl/md5/{md5}"})
        return results
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "Japaneasy Tim Anderson"
    print(json.dumps(search(query)))
