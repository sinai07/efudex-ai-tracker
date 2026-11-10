import csv
import io
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import boto3

# ---------- Settings ----------
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
MODEL_ID = "amazon.nova-micro-v1:0"
LABELS = ["not_relevant", "expected", "notable", "potentially_serious", "needs_review"]
BUCKET = "<YOUR_BUCKET_NAME>"  # change to your bucket name
TOPIC_ARN = "arn:aws:sns:us-east-1:<YOUR_ACCOUNT_ID>:efudex-tracker-alerts"  # change to your topic ARN

# Order used in reports: most important first
SEVERITY_ORDER = ["potentially_serious", "notable", "needs_review", "expected", "not_relevant"]

QUERY = (
    '("fluorouracil"[tiab] OR "5-fluorouracil"[tiab] OR "5-FU"[tiab] OR "Efudex"[tiab]) '
    'AND (topical[tiab] OR cream[tiab]) '
    'AND (skin[tiab] OR cutaneous[tiab] OR dermatitis[tiab] OR reaction*[tiab] OR adverse[tiab])'
)

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
s3 = boto3.client("s3")
sns = boto3.client("sns", region_name="us-east-1")

# ---------- Instructions for the AI ----------
SYSTEM_PROMPT = """You screen PubMed abstracts for skin reactions to TOPICAL fluorouracil (5-FU), especially 5% cream (Efudex).

Choose exactly one label:
- not_relevant: not about topical fluorouracil, or no skin reaction to it is reported (e.g., systemic/IV 5-FU, other drugs, efficacy only).
- expected: usual local effects within the treated area (redness, crusting, burning, itching, erosion).
- notable: beyond the usual effect (e.g., allergic contact dermatitis, spread beyond the treated area, pigment changes).
- potentially_serious: severe outcomes (e.g., ulceration, necrosis, secondary infection, hospitalization, SJS/TEN, systemic toxicity).
- needs_review: the formulation or route is unclear, or the abstract does not give enough information to decide.

Rules:
- Use only the abstract. Do not use outside knowledge about the patient.
- evidence_quote must be copied WORD FOR WORD from the abstract (one sentence or less). Use "" if the label is not_relevant.
- Answer with JSON only, no other text:
{"label": "...", "formulation": "...", "reaction": "...", "evidence_quote": "...", "reason": "..."}"""


# ---------- PubMed (Step 1) ----------
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "efudex-tracker-lab"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def search_pubmed(days):
    params = urllib.parse.urlencode({
        "db": "pubmed", "term": QUERY, "datetype": "edat",
        "reldate": days, "retmax": 100, "retmode": "json",
    })
    data = json.loads(get(f"{EUTILS}/esearch.fcgi?{params}"))
    return data["esearchresult"]["idlist"]


def fetch_articles(pmids):
    if not pmids:
        return []
    params = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
    root = ET.fromstring(get(f"{EUTILS}/efetch.fcgi?{params}"))
    articles = []
    for item in root.findall(".//PubmedArticle"):
        pmid = item.findtext(".//PMID")
        title_el = item.find(".//ArticleTitle")
        title = "".join(title_el.itertext()) if title_el is not None else ""
        abstract = " ".join("".join(p.itertext()) for p in item.findall(".//AbstractText"))
        articles.append({
            "pmid": pmid,
            "title": title.strip(),
            "abstract": abstract.strip(),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return articles


# ---------- AI classification (Step 2) ----------
def normalize(text):
    """Lowercase and collapse spaces, so small formatting differences don't matter."""
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_json(text):
    """Extract the JSON object from the model's answer."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0))


def classify(article):
    if not article["abstract"]:
        return {"label": "needs_review", "formulation": "", "reaction": "",
                "evidence_quote": "", "reason": "No abstract available."}

    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text":
            f"Title: {article['title']}\n\nAbstract: {article['abstract']}"}]}],
        inferenceConfig={"maxTokens": 400, "temperature": 0},
    )
    answer = response["output"]["message"]["content"][0]["text"]
    result = parse_json(answer)

    # Safety checks: the code does not blindly trust the AI.
    if result.get("label") not in LABELS:
        result["reason"] = f"Invalid label from model: {result.get('label')}"
        result["label"] = "needs_review"

    quote = result.get("evidence_quote", "")
    if result["label"] in ("expected", "notable", "potentially_serious"):
        if not quote or normalize(quote) not in normalize(article["abstract"]):
            result["reason"] = "Evidence quote not found in the abstract. " + result.get("reason", "")
            result["label"] = "needs_review"

    return result


# ---------- Reports in S3 (Step 3) ----------
def save_reports(summary):
    """Save the results as JSON and CSV in s3://BUCKET/reports/YYYY-MM-DD/."""
    folder = f"reports/{summary['run_date']}"

    json_key = f"{folder}/report.json"
    s3.put_object(
        Bucket=BUCKET, Key=json_key,
        Body=json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )

    columns = ["label", "pmid", "title", "formulation", "reaction",
               "evidence_quote", "reason", "url"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(summary["results"])

    csv_key = f"{folder}/report.csv"
    s3.put_object(
        Bucket=BUCKET, Key=csv_key,
        Body=buffer.getvalue().encode("utf-8-sig"),  # utf-8-sig so Excel shows special characters correctly
        ContentType="text/csv",
    )
    return [json_key, csv_key]


# ---------- Email with SNS (Step 4) ----------
SECTION_TITLES = {
    "potentially_serious": "🔴 POTENTIALLY SERIOUS",
    "notable": "🟡 NOTABLE",
    "needs_review": "🔍 NEEDS REVIEW",
    "expected": "🟢 EXPECTED",
    "not_relevant": "⚪ NOT RELEVANT",
}


def send_email(summary):
    """Send a plain-text summary with a link to every article."""
    counts = summary["counts"]
    lines = [
        "AI Drug Side Effect Tracker – Weekly Report",
        f"Run date: {summary['run_date']} | Period: last {summary['days_searched']} days",
        f"Articles found: {summary['articles_found']}",
        "",
    ]

    if summary["articles_found"] == 0:
        lines += ["No new articles matched the search this period.", ""]

    for label in SEVERITY_ORDER if summary["articles_found"] else []:
        group = [r for r in summary["results"] if r["label"] == label]
        lines.append(f"{SECTION_TITLES[label]} ({len(group)})")
        for r in group:
            lines.append(f"- {r['title']}")
            if label != "not_relevant":
                lines.append(f"  Reaction: {r.get('reaction', '')}")
                lines.append(f"  Formulation: {r.get('formulation', '')}")
                if r.get("evidence_quote"):
                    lines.append(f'  Evidence: "{r["evidence_quote"]}"')
                if label == "needs_review":
                    lines.append(f"  Reason: {r.get('reason', '')}")
            lines.append(f"  Link: {r['url']}")
        lines.append("")

    lines += [
        f"Reports saved in S3: s3://{BUCKET}/reports/{summary['run_date']}/",
        "",
        "This is an educational prototype. Every result must be verified by a person.",
    ]

    subject = (f"Efudex Tracker: {counts['potentially_serious']} potentially serious, "
               f"{counts['notable']} notable ({summary['articles_found']} articles)")
    sns.publish(TopicArn=TOPIC_ARN, Subject=subject[:100], Message="\n".join(lines))


# ---------- Main ----------
def lambda_handler(event, context):
    days = int(event.get("days", 7))
    articles = fetch_articles(search_pubmed(days))

    results = []
    for article in articles:
        try:
            analysis = classify(article)
        except Exception as error:  # one bad article must not stop the whole run
            analysis = {"label": "needs_review", "formulation": "", "reaction": "",
                        "evidence_quote": "", "reason": f"Analysis error: {error}"}
        results.append({
            "pmid": article["pmid"],
            "title": article["title"],
            "url": article["url"],
            **analysis,
        })

    # Most important articles first
    results.sort(key=lambda r: SEVERITY_ORDER.index(r["label"]))

    summary = {
        "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "days_searched": days,
        "articles_found": len(results),
        "counts": {label: sum(1 for r in results if r["label"] == label) for label in LABELS},
        "results": results,
    }
    summary["report_files"] = save_reports(summary)
    send_email(summary)

    print(json.dumps(summary, indent=2))
    return summary
