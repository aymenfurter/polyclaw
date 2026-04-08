"""Foundry IQ service -- Azure AI Search indexing and search for agent memories."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from ..config.settings import cfg
from ..state.foundry_iq_config import FoundryIQConfigStore, get_foundry_iq_config

logger = logging.getLogger(__name__)

SEARCH_API_VERSION = "2024-07-01"


def _get_embedding(text: str, config: FoundryIQConfigStore) -> list[float]:
    c = config.config
    url = (
        f"{c.embedding_endpoint.rstrip('/')}/openai/deployments/"
        f"{c.embedding_model}/embeddings?api-version=2024-10-21"
    )
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if c.embedding_api_key:
        headers["api-key"] = c.embedding_api_key
    else:
        headers["Authorization"] = f"Bearer {_get_entra_token()}"

    resp = requests.post(url, headers=headers, json={"input": text, "model": c.embedding_model}, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def _get_entra_token() -> str:
    from azure.identity import DefaultAzureCredential  # type: ignore[import-untyped]

    credential = DefaultAzureCredential()
    return credential.get_token("https://cognitiveservices.azure.com/.default").token


def _search_headers(config: FoundryIQConfigStore) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if config.config.search_api_key:
        headers["api-key"] = config.config.search_api_key
    else:
        headers["Authorization"] = f"Bearer {_get_search_token()}"
    return headers


def _get_search_token() -> str:
    from azure.identity import DefaultAzureCredential  # type: ignore[import-untyped]

    credential = DefaultAzureCredential()
    return credential.get_token("https://search.azure.com/.default").token


def _search_url(config: FoundryIQConfigStore, path: str) -> str:
    base = config.config.search_endpoint.rstrip("/")
    return f"{base}/{path}?api-version={SEARCH_API_VERSION}"


def ensure_index(config: FoundryIQConfigStore | None = None) -> dict[str, Any]:
    config = config or get_foundry_iq_config()
    c = config.config
    index_def = {
        "name": c.index_name,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "content", "type": "Edm.String", "searchable": True},
            {"name": "title", "type": "Edm.String", "searchable": True, "filterable": True},
            {"name": "source_type", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "source_path", "type": "Edm.String", "filterable": True},
            {"name": "date", "type": "Edm.String", "filterable": True, "sortable": True},
            {"name": "indexed_at", "type": "Edm.DateTimeOffset", "filterable": True, "sortable": True},
            {
                "name": "content_vector", "type": "Collection(Edm.Single)",
                "searchable": True, "dimensions": c.embedding_dimensions,
                "vectorSearchProfile": "vector-profile",
            },
        ],
        "vectorSearch": {
            "algorithms": [{"name": "hnsw-algo", "kind": "hnsw"}],
            "profiles": [{"name": "vector-profile", "algorithm": "hnsw-algo"}],
        },
        "semantic": {
            "defaultConfiguration": "semantic-config",
            "configurations": [{
                "name": "semantic-config",
                "prioritizedFields": {
                    "prioritizedContentFields": [{"fieldName": "content"}],
                    "titleField": {"fieldName": "title"},
                },
            }],
        },
    }

    url = _search_url(config, f"indexes/{c.index_name}")
    try:
        resp = requests.put(url, headers=_search_headers(config), json=index_def, timeout=30)
    except Exception as exc:
        return {"status": "error", "message": f"Failed to connect to search service: {exc}", "detail": str(exc), "url": url}
    if resp.status_code in (200, 201, 204):
        return {"status": "ok", "index": c.index_name}
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:500]
    return {"status": "error", "message": f"Index creation failed (HTTP {resp.status_code})", "detail": body, "code": resp.status_code, "url": url}


def delete_index(config: FoundryIQConfigStore | None = None) -> dict[str, Any]:
    config = config or get_foundry_iq_config()
    try:
        resp = requests.delete(
            _search_url(config, f"indexes/{config.config.index_name}"),
            headers=_search_headers(config), timeout=30,
        )
    except Exception as exc:
        return {"status": "error", "message": f"Connection failed: {exc}"}
    if resp.status_code in (200, 204):
        return {"status": "ok"}
    return {"status": "error", "message": f"Delete failed (HTTP {resp.status_code}): {resp.text[:300]}"}


def get_index_stats(config: FoundryIQConfigStore | None = None) -> dict[str, Any]:
    config = config or get_foundry_iq_config()
    if not config.is_configured:
        return {"status": "ok", "document_count": 0, "storage_size": 0, "index_missing": True}
    try:
        resp = requests.get(
            _search_url(config, f"indexes/{config.config.index_name}/stats"),
            headers=_search_headers(config), timeout=15,
        )
    except Exception as exc:
        return {"status": "error", "message": f"Connection failed: {exc}"}
    if resp.ok:
        data = resp.json()
        return {"status": "ok", "document_count": data.get("documentCount", 0), "storage_size": data.get("storageSize", 0)}
    if resp.status_code == 404:
        return {"status": "ok", "document_count": 0, "storage_size": 0, "index_missing": True}
    return {"status": "error", "message": f"Stats failed (HTTP {resp.status_code}): {resp.text[:300]}"}


def _discover_memory_files() -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    daily_dir = cfg.memory_daily_dir
    if daily_dir.is_dir():
        for f in sorted(daily_dir.glob("*.md")):
            files.append({"path": str(f), "title": f"Daily Log - {f.stem}", "source_type": "daily", "date": f.stem})
    topics_dir = cfg.memory_topics_dir
    if topics_dir.is_dir():
        for f in sorted(topics_dir.glob("*.md")):
            files.append({
                "path": str(f),
                "title": f"Topic - {f.stem.replace('-', ' ').title()}",
                "source_type": "topic", "date": "",
            })
    return files


def _file_to_doc_id(path: str) -> str:
    return hashlib.md5(path.encode()).hexdigest()


def _chunk_text(text: str, max_chars: int = 4000) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > max_chars:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks if chunks else [text[:max_chars]]


def index_memories(config: FoundryIQConfigStore | None = None) -> dict[str, Any]:
    config = config or get_foundry_iq_config()
    if not config.is_configured:
        c = config.config
        missing = []
        if not c.search_endpoint:
            missing.append("search_endpoint")
        if not c.search_api_key and not c.provisioned:
            missing.append("search_api_key")
        if not c.embedding_endpoint:
            missing.append("embedding_endpoint")
        if not c.embedding_api_key and not c.provisioned:
            missing.append("embedding_api_key")
        return {"status": "error", "message": f"Foundry IQ not configured -- missing: {', '.join(missing) or 'unknown'}"}

    idx_result = ensure_index(config)
    if idx_result.get("status") == "error":
        return idx_result

    files = _discover_memory_files()
    if not files:
        return {"status": "ok", "indexed": 0, "message": "No memory files found"}

    documents: list[dict[str, Any]] = []
    errors: list[str] = []
    now = datetime.now(UTC).isoformat()

    for file_info in files:
        try:
            content = Path(file_info["path"]).read_text(errors="replace")
            if not content.strip():
                continue
            chunks = _chunk_text(content)
            for i, chunk in enumerate(chunks):
                doc_id = _file_to_doc_id(file_info["path"]) + (f"-{i}" if i > 0 else "")
                try:
                    embedding = _get_embedding(chunk, config)
                except Exception as exc:
                    errors.append(f"Embedding failed for {file_info['path']}[{i}]: {exc}")
                    continue
                documents.append({
                    "@search.action": "mergeOrUpload", "id": doc_id, "content": chunk,
                    "title": file_info["title"], "source_type": file_info["source_type"],
                    "source_path": file_info["path"], "date": file_info["date"],
                    "indexed_at": now, "content_vector": embedding,
                })
        except Exception as exc:
            errors.append(f"Failed to read {file_info['path']}: {exc}")

    if not documents:
        return {"status": "ok", "indexed": 0, "message": "No documents to index", "errors": errors}

    indexed = 0
    batch_size = 100
    for start in range(0, len(documents), batch_size):
        batch = documents[start:start + batch_size]
        resp = requests.post(
            _search_url(config, f"indexes/{config.config.index_name}/docs/index"),
            headers=_search_headers(config), json={"value": batch}, timeout=60,
        )
        if resp.ok:
            result = resp.json()
            indexed += sum(1 for r in result.get("value", []) if r.get("statusCode") in (200, 201))
        else:
            errors.append(f"Batch upload failed: {resp.status_code} {resp.text[:200]}")

    config.set_last_indexed(now)
    return {"status": "ok", "indexed": indexed, "total_files": len(files), "total_chunks": len(documents), "errors": errors}


def search_memories(query: str, top: int = 5, config: FoundryIQConfigStore | None = None) -> dict[str, Any]:
    config = config or get_foundry_iq_config()
    if not config.is_configured:
        return {"status": "error", "message": "Foundry IQ not configured (run provisioning first)"}
    if not config.enabled:
        return {"status": "error", "message": "Foundry IQ is not enabled (enable it in admin settings)"}

    try:
        query_vector = _get_embedding(query, config)
    except Exception as exc:
        logger.error("Failed to generate query embedding: %s", exc)
        return {"status": "error", "message": f"Embedding generation failed: {exc}"}

    search_body: dict[str, Any] = {
        "search": query, "queryType": "semantic", "semanticConfiguration": "semantic-config",
        "top": top, "select": "id,content,title,source_type,date,source_path",
        "vectorQueries": [{"kind": "vector", "vector": query_vector, "fields": "content_vector", "k": top}],
    }

    resp = requests.post(
        _search_url(config, f"indexes/{config.config.index_name}/docs/search"),
        headers=_search_headers(config), json=search_body, timeout=30,
    )
    if not resp.ok:
        return {"status": "error", "message": f"Search failed: {resp.status_code} {resp.text[:200]}"}

    data = resp.json()
    results = [
        {
            "title": doc.get("title", ""), "content": doc.get("content", ""),
            "source_type": doc.get("source_type", ""), "date": doc.get("date", ""),
            "score": doc.get("@search.score", 0), "reranker_score": doc.get("@search.rerankerScore", 0),
        }
        for doc in data.get("value", [])
    ]
    return {"status": "ok", "results": results, "count": len(results)}


def test_search_connection(config: FoundryIQConfigStore | None = None) -> dict[str, Any]:
    config = config or get_foundry_iq_config()
    try:
        resp = requests.get(_search_url(config, "indexes"), headers=_search_headers(config), timeout=10)
        if resp.ok:
            indexes = resp.json().get("value", [])
            return {"status": "ok", "message": f"Connected. {len(indexes)} index(es) found.", "indexes": [i["name"] for i in indexes]}
        return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def test_embedding_connection(config: FoundryIQConfigStore | None = None) -> dict[str, Any]:
    config = config or get_foundry_iq_config()
    try:
        embedding = _get_embedding("Hello test", config)
        return {"status": "ok", "message": f"Connected. Embedding dimension: {len(embedding)}", "dimensions": len(embedding)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
